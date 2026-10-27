import asyncio
import logging
import re
import secrets
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.adapters.base import AdapterResult, UserPayload, coerce_unix_ts
from app.adapters.factory import get_adapter
from app.core.config import get_settings
from app.models.group import GroupPanel, UserGroup
from app.models.panel import Panel, PanelType
from app.models.user import PanelAccount, PanelAccountStatus, User, UserStatus
from app.services import quota as quota_math

logger = logging.getLogger("astra.usage")

# Marzban / PasarGuard username: 3–32 of [a-z0-9_]
_REMOTE_USER_RE = re.compile(r"^[a-z0-9_]{3,32}$")

# Per-user locks to avoid stampede on concurrent sub fetches
_user_locks: dict[int, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()

# Shared cap for parallel remote create/delete/modify (lazy-init for event loop)
_panel_remote_sem: asyncio.Semaphore | None = None
# One admin/heal bulk retry at a time — avoids stampede on a recovering panel
_bulk_retry_lock = asyncio.Lock()


def _remote_sem() -> asyncio.Semaphore:
    global _panel_remote_sem
    if _panel_remote_sem is None:
        n = int(getattr(get_settings(), "PANEL_REMOTE_CONCURRENCY", 20) or 20)
        _panel_remote_sem = asyncio.Semaphore(max(1, min(n, 64)))
    return _panel_remote_sem


def _is_not_found_err(err: str | None) -> bool:
    low = (err or "").lower()
    return any(
        x in low
        for x in (
            "not found",
            "does not exist",
            "doesn't exist",
            "user not found",
            "404",
            "یافت نشد",
            "پیدا نشد",
            "وجود ندارد",
        )
    )


def _is_unreachable_err(err: str | None) -> bool:
    """Network / timeout / DNS — remote panel cannot be reached right now."""
    low = (err or "").lower()
    return any(
        x in low
        for x in (
            "timeout",
            "timed out",
            "time out",
            "connection",
            "connect",
            "unreachable",
            "refused",
            "reset by peer",
            "name or service not known",
            "nodename nor servname",
            "network is unreachable",
            "temporary failure",
            "ssl",
            "certificate",
            "handshake",
            "proxy error",
            "503",
            "502",
            "504",
            "قطع",
            "در دسترس",
            "زمان تمام",
            "پاسخ نداد",
        )
    )


def _is_hard_down_err(err: str | None) -> bool:
    """Clear permanent/unreachable failure — safe to skip without retrying accounts."""
    low = (err or "").lower()
    return any(
        x in low
        for x in (
            "name or service not known",
            "nodename nor servname",
            "getaddrinfo failed",
            "no address associated",
            "connection refused",
            "no route to host",
            "network is unreachable",
            "unauthorized",
            "forbidden",
            "invalid credentials",
            "authentication failed",
            "login failed",
            "401",
            "403",
        )
    )


async def _verify_remote_user_exists(
    adapter,
    username: str,
    remote_id: str | None = None,
    *,
    timeout: float = 6.0,
) -> AdapterResult | None:
    """If the remote user already exists, return its usage result (clears false errors)."""
    uname = (username or "").strip()
    if not uname:
        return None
    try:
        try:
            coro = adapter.get_usage(uname, remote_id, expand_links=False)  # type: ignore[call-arg]
        except TypeError:
            try:
                coro = adapter.get_usage(uname, expand_links=False)  # type: ignore[call-arg]
            except TypeError:
                coro = adapter.get_usage(uname)
        res = await asyncio.wait_for(coro, timeout=timeout)
        if res and res.success:
            return res
    except Exception:  # noqa: BLE001
        return None
    return None


def _is_username_taken_err(err: str | None) -> bool:
    """Remote rejected create because username is reserved / owned elsewhere."""
    low = (err or "").lower()
    if "409" in low:
        return True
    return any(
        x in low
        for x in (
            "already exist",
            "already exists",
            "user already",
            "duplicate",
            "تکرار",
            "موجود",
        )
    )


def sanitize_remote_username(name: str) -> str:
    """Force Marzban-family username rules (a-z 0-9 _, 3–32)."""
    s = re.sub(r"[^a-z0-9_]", "_", (name or "").strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    if len(s) < 3:
        s = (s + "usr")[:3]
    return s[:32]


def make_remote_username_alias(
    astra_username: str,
    *,
    panel_id: int,
    attempt: int = 0,
) -> str:
    """Unique remote name clearly tied to Astra user + panel.

    attempt 0 → `{base}_p{panel_id}`  e.g. testttt_p2
    attempt 1+ → `{base}_p{panel_id}{hex}` so retries never collide
    """
    base = sanitize_remote_username(astra_username)
    if attempt <= 0:
        suffix = f"_p{int(panel_id)}"
    else:
        suffix = f"_p{int(panel_id)}{secrets.token_hex(1)}"
    keep = max(3, 32 - len(suffix))
    candidate = f"{base[:keep]}{suffix}"
    return sanitize_remote_username(candidate)[:32]


def make_group_remote_username(astra_username: str, group_id: int) -> str:
    """Remote name when several Astra groups share one panel: ``{base}_g{group_id}``."""
    base = sanitize_remote_username(astra_username)
    suffix = f"_g{int(group_id)}"
    keep = max(3, 32 - len(suffix))
    return sanitize_remote_username(f"{base[:keep]}{suffix}")[:32]


def _astra_note(user: User, remote_username: str) -> str:
    """Tag remote note so panel admins see Astra ownership."""
    base = (user.note or "").strip()
    tag = f"Astra:{user.username}"
    if remote_username and remote_username != user.username:
        tag = f"{tag}→{remote_username}"
    if tag in base:
        return base
    return f"{tag} | {base}".strip(" |") if base else tag


def _format_remote_error(
    panel: Panel | None,
    *,
    action: str,
    err: str | None,
    username: str | None = None,
) -> str:
    """Human-readable panel error for admin UI (Persian + original detail)."""
    pname = (panel.name if panel else None) or "پنل"
    ptype = ""
    if panel is not None:
        raw = getattr(panel, "type", None)
        ptype = raw.value if hasattr(raw, "value") else (str(raw) if raw else "")
    base = ""
    if panel is not None:
        base = str(getattr(panel, "base_url", "") or "").rstrip("/")
    detail = (err or "").strip() or "خطای نامشخص"
    parts = [f"عملیات: {action}", f"پنل: {pname}"]
    if ptype:
        parts.append(f"نوع: {ptype}")
    if username:
        parts.append(f"یوزرنیم ریموت: {username}")
    if base:
        parts.append(f"آدرس: {base}")
    if _is_username_taken_err(detail):
        parts.append(
            "علت: یوزرنیم روی این پنل تکراری/رزرو است "
            "(احتمالاً ادمین دیگر). Astra باید با نام یکتا مثل name_p{panel} بسازد."
        )
        parts.append(f"پاسخ پنل: {detail}")
    elif _is_not_found_err(detail):
        parts.append(
            "علت: کاربر روی این پنل وجود ندارد "
            "(هنوز ساخته نشده یا از پنل حذف شده)."
        )
        parts.append(f"پاسخ پنل: {detail}")
    else:
        parts.append(f"پاسخ پنل: {detail}")
    return "\n".join(parts)


def coefficient_from_config(config: dict | None) -> float:
    return quota_math.norm_coeff((config or {}).get("coefficient", 1))


def _norm_coeff(raw: float | None, default: float = 1.0) -> float:
    return quota_math.norm_coeff(raw, default)


def account_weighted_usage(account: PanelAccount, coeff: float) -> int:
    """Astra-facing usage for one panel: raw × live coefficient."""
    return quota_math.weighted_bytes(int(account.used_traffic or 0), coeff)


def apply_coefficient_period(account: PanelAccount, new_coeff: float) -> bool:
    """Keep applied_coefficient in sync; always recount full raw × live coeff."""
    new_c = _norm_coeff(new_coeff)
    # Clear legacy anchors so old period-lock never under-counts
    dirty = False
    if int(getattr(account, "coeff_anchor_raw", 0) or 0) or int(
        getattr(account, "coeff_anchor_weighted", 0) or 0
    ):
        account.coeff_anchor_raw = 0
        account.coeff_anchor_weighted = 0
        dirty = True

    applied = getattr(account, "applied_coefficient", None)
    if applied is None or abs(_norm_coeff(applied) - new_c) >= 1e-9:
        account.applied_coefficient = new_c
        return True
    return dirty


def remote_data_limit_bytes(
    user_limit: int,
    config: dict | None,
    *,
    used_weighted_total: int = 0,
    used_raw_on_panel: int = 0,
) -> int:
    """Full purchased quota on every remote (coefficient does not shrink caps)."""
    return quota_math.remote_data_limit(
        user_limit,
        used_weighted_total=used_weighted_total,
        used_raw_on_panel=used_raw_on_panel,
        coefficient=coefficient_from_config(config),
    )


def _remote_status_for_user(user: User) -> str:
    """Status string pushed to remotes. limited/expired cut traffic even if remote has leftover."""
    st = user.status
    if st in (UserStatus.limited, UserStatus.expired, UserStatus.disabled):
        return st.value
    if st == UserStatus.on_hold:
        return UserStatus.on_hold.value
    return UserStatus.active.value


def build_user_payload(
    user: User,
    *,
    uuid: str | None,
    panel_config: dict | None,
    used_weighted_total: int | None = None,
    used_raw_on_panel: int = 0,
    remote_username: str | None = None,
) -> UserPayload:
    """Build remote payload.

    Active: every panel gets the full Astra data_limit (e.g. 4GB everywhere).
    Limited/expired/disabled: freeze at current raw + disabled status on ALL panels.
    Coefficient only affects Astra usage Σ(raw×coeff), not create caps.

    remote_username: optional per-panel alias when Astra name is taken on that panel.
    """
    cfg = {
        **(panel_config or {}),
        "proxy_settings": user.proxy_settings or {},
        "data_limit_reset_strategy": user.data_limit_reset_strategy,
    }
    explicit_proxies = (user.proxy_settings or {}).get("proxies")
    if isinstance(explicit_proxies, dict) and explicit_proxies:
        cfg["proxies"] = explicit_proxies
    used_w = (
        int(used_weighted_total)
        if used_weighted_total is not None
        else int(user.used_traffic or 0)
    )
    status = _remote_status_for_user(user)
    hold_dur = 0
    try:
        hold_dur = max(0, int(getattr(user, "on_hold_expire_duration", 0) or 0))
    except (TypeError, ValueError):
        hold_dur = 0
    if hold_dur > 0:
        cfg["on_hold_expire_duration"] = hold_dur
    data_limit = remote_data_limit_bytes(
        user.data_limit or 0,
        cfg,
        used_weighted_total=used_w,
        used_raw_on_panel=used_raw_on_panel,
    )
    # Sale finished → freeze every remote so leftover raw bytes cannot be used
    if status in ("limited", "expired", "disabled"):
        used_r = max(0, int(used_raw_on_panel or 0))
        if int(user.data_limit or 0) > 0:
            data_limit = max(1, used_r) if used_r > 0 else 1
    uname = sanitize_remote_username(remote_username or user.username)
    expire = user.expire or None
    # Marzban / PasarGuard / Rebecca reject expire together with on_hold
    if status == "on_hold":
        expire = None
    return UserPayload(
        username=uname,
        data_limit=data_limit,
        expire=expire,
        status=status,
        note=_astra_note(user, uname),
        uuid=uuid,
        config=cfg,
    )


async def create_user_with_collision_alias(
    adapter,
    payload: UserPayload,
    *,
    astra_username: str,
    panel_id: int,
    timeout: float = 30.0,
    max_alias_attempts: int = 2,
    probe_timeout: float = 4.0,
) -> AdapterResult:
    """Create on remote; if username taken/unreadable, retry with `{name}_p{panel}` aliases.

    ``max_alias_attempts``: how many alias tries after the preferred name
    (0 = preferred only). Keep small on the create-user HTTP path so a dead
    panel cannot stall for minutes.
    """
    preferred = sanitize_remote_username(payload.username or astra_username)
    payload.username = preferred
    payload.note = payload.note  # already tagged in build_user_payload

    last = _fail_result("create failed")
    alias_limit = max(0, int(max_alias_attempts))
    # attempt -1 = preferred name; 0..alias_limit-1 = aliases
    for attempt in range(-1, alias_limit):
        if attempt >= 0:
            alias = make_remote_username_alias(
                astra_username, panel_id=panel_id, attempt=attempt
            )
            if alias == payload.username:
                alias = make_remote_username_alias(
                    astra_username, panel_id=panel_id, attempt=attempt + 1
                )
            payload.username = alias
            # Refresh note tag for new alias
            # (payload.note may already contain Astra: — keep short)
            if payload.note and "Astra:" in str(payload.note):
                payload.note = re.sub(
                    r"Astra:\S+",
                    f"Astra:{astra_username}→{alias}",
                    str(payload.note),
                    count=1,
                )
            else:
                payload.note = f"Astra:{astra_username}→{alias}"
            logger.info(
                "create alias retry panel=%s astra=%s remote=%s attempt=%s",
                panel_id,
                astra_username,
                alias,
                attempt,
            )
        try:
            last = await asyncio.wait_for(adapter.create_user(payload), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            last = _fail_result(_exc_msg(e))
            # Timeout: confirm preferred/alias might already exist (short probe)
            try:
                usage = await asyncio.wait_for(
                    adapter.get_usage(payload.username, expand_links=False),  # type: ignore[call-arg]
                    timeout=probe_timeout,
                )
                if usage and usage.success:
                    return usage
            except TypeError:
                try:
                    usage = await asyncio.wait_for(
                        adapter.get_usage(payload.username), timeout=probe_timeout
                    )
                    if usage and usage.success:
                        return usage
                except Exception:
                    pass
            except Exception:
                pass
            continue

        if last.success:
            # Ensure remote_username is the name we actually used
            if not last.remote_username:
                last.remote_username = payload.username
            return last

        # Readable "already exists" that we could use would have succeeded inside adapter.
        # Taken / conflict → try alias. Other errors → stop (except first preferred try).
        if _is_username_taken_err(last.error) or (
            attempt < 0 and _is_not_found_err(last.error)
        ):
            continue
        # Non-collision failure on preferred name: still try one alias (some panels
        # return opaque errors for taken names)
        if attempt < 0 and alias_limit > 0:
            continue
        break

    return last


async def _lock_for_user(user_id: int) -> asyncio.Lock:
    async with _locks_guard:
        lock = _user_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            _user_locks[user_id] = lock
        return lock


def merge_group_panel_configs(a: dict, b: dict) -> dict:
    """Merge two GroupPanel configs (legacy helper for scripts / probes).

    Prefer per-group PanelAccounts — coefficients must stay independent.
    """
    out = {**a, **b}

    # protocols: union preserving order
    pa = a.get("protocols") if isinstance(a.get("protocols"), list) else []
    pb = b.get("protocols") if isinstance(b.get("protocols"), list) else []
    if pa or pb:
        seen_p: list[str] = []
        for p in list(pa) + list(pb):
            s = str(p).lower().strip()
            if s and s not in seen_p:
                seen_p.append(s)
        out["protocols"] = seen_p

    # inbounds: per-protocol tag union
    ia = a.get("inbounds") if isinstance(a.get("inbounds"), dict) else {}
    ib = b.get("inbounds") if isinstance(b.get("inbounds"), dict) else {}
    if ia or ib:
        merged_ib: dict[str, list[str]] = {}
        for proto in list(ia.keys()) + list(ib.keys()):
            key = str(proto).lower().strip()
            if not key:
                continue
            tags: list[str] = []
            for src in (ia.get(proto), ia.get(key), ib.get(proto), ib.get(key)):
                if isinstance(src, list):
                    tags.extend(str(t).strip() for t in src if t and str(t).strip())
                elif isinstance(src, str) and src.strip():
                    tags.append(src.strip())
            uniq: list[str] = []
            for t in tags:
                if t not in uniq:
                    uniq.append(t)
            if uniq:
                merged_ib[key] = uniq
        out["inbounds"] = merged_ib

    # PasarGuard group ids: union
    for key in ("remote_group_ids", "group_ids"):
        va = a.get(key) if isinstance(a.get(key), list) else []
        vb = b.get(key) if isinstance(b.get(key), list) else []
        if va or vb:
            ids: list[int] = []
            for x in list(va) + list(vb):
                try:
                    i = int(x)
                except (TypeError, ValueError):
                    continue
                if i not in ids:
                    ids.append(i)
            out[key] = ids

    out["coefficient"] = max(coefficient_from_config(a), coefficient_from_config(b))
    return out


async def resolve_panels_for_groups(
    db: AsyncSession, group_ids: list[int]
) -> list[tuple[Panel, dict, int]]:
    """Return one (panel, config, group_id) per GroupPanel link.

    Same panel in multiple groups → multiple targets (separate remote accounts)
    so each group's coefficient applies only to that group's traffic.
    Skips inactive panels and disabled sales groups.
    """
    if not group_ids:
        return []
    from app.models.group import Group

    result = await db.execute(
        select(GroupPanel)
        .join(Group, Group.id == GroupPanel.group_id)
        .where(
            GroupPanel.group_id.in_(group_ids),
            Group.is_disabled.is_(False),
        )
        .options(selectinload(GroupPanel.panel))
        .order_by(GroupPanel.group_id.asc(), GroupPanel.priority.desc())
    )
    links = result.scalars().all()
    out: list[tuple[Panel, dict, int]] = []
    seen_keys: set[tuple[int, int]] = set()
    for link in links:
        panel = link.panel
        if not panel or not panel.is_active:
            continue
        gid = int(link.group_id)
        key = (int(panel.id), gid)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        out.append((panel, dict(link.config or {}), gid))
    return out


def _normalize_panel_targets(
    panels: list[tuple[Panel, dict] | tuple[Panel, dict, int]],
) -> list[tuple[Panel, dict, int]]:
    """Accept legacy (panel, cfg) or new (panel, cfg, group_id) tuples."""
    out: list[tuple[Panel, dict, int]] = []
    for item in panels:
        if len(item) >= 3:
            panel, cfg, gid = item[0], item[1], int(item[2])
        else:
            panel, cfg = item[0], item[1]
            gid = 0
        out.append((panel, dict(cfg or {}), gid))
    return out


def _exc_msg(exc: BaseException) -> str:
    """TimeoutError and some OS errors have empty str() — always return something useful."""
    msg = str(exc).strip()
    if msg:
        return msg
    if isinstance(exc, asyncio.TimeoutError):
        return "Timeout waiting for panel response"
    return exc.__class__.__name__


def _fail_result(error: str) -> AdapterResult:
    return AdapterResult(
        success=False,
        error=error or "Unknown panel error",
        links=[],
        used_traffic=0,
        remote_username=None,
        remote_id=None,
        uuid=None,
    )


# Soft marker: overall budget cancelled an in-flight create — not a real panel fault.
_PROVISION_BUDGET_SOFT = "__PROVISION_BUDGET__"


async def provision_user_on_panels(
    db: AsyncSession,
    user: User,
    panels: list[tuple[Panel, dict] | tuple[Panel, dict, int]],
    *,
    fast: bool = False,
    overall_timeout: float | None = None,
) -> list[PanelAccount]:
    """Create one remote account per (panel × group) in parallel.

    When several Astra groups share a panel, each group gets its own remote
    user (``username_g{id}``) so raw usage × that group's coefficient is exact.

    Remote failures never raise — those accounts stay ``error`` so Astra user
    still exists and a later heal/retry can finish them.

    ``fast=True`` (create-user HTTP path): all targets start together (high
    concurrency). Per-panel timeout is generous enough for PasarGuard; overall
    budget scales with waves so a healthy-but-slow panel is not falsely marked
    error. Budget-cancelled rows stay ``pending`` for auto-heal (not fake errors).
    """
    targets = _normalize_panel_targets(panels)
    if not targets:
        return []

    t_prov = time.monotonic()
    n_targets = len(targets)
    # Parallel create: enough slots so most/all panels start at once.
    # Global shared sem is too small when many groups × panels queue up.
    if fast:
        create_timeout = 20.0
        alias_attempts = 1
        probe_timeout = 4.0
        sem_limit = max(24, min(48, n_targets))
        waves = max(1, (n_targets + sem_limit - 1) // sem_limit)
        # One wave ≈ one create_timeout; do not use a flat 22s that cancels wave 2+.
        auto_budget = min(85.0, waves * create_timeout + 10.0)
        budget = float(overall_timeout) if overall_timeout is not None else auto_budget
        hydrate_budget = 8.0
        sem: asyncio.Semaphore = asyncio.Semaphore(sem_limit)
    else:
        create_timeout = 25.0
        alias_attempts = 2
        probe_timeout = 5.0
        sem_limit = max(16, min(40, n_targets))
        waves = max(1, (n_targets + sem_limit - 1) // sem_limit)
        auto_budget = min(120.0, waves * create_timeout + 15.0)
        budget = float(overall_timeout) if overall_timeout is not None else auto_budget
        hydrate_budget = 14.0
        sem = asyncio.Semaphore(sem_limit)

    panel_ids = list({p.id for p, _, _ in targets})
    existing_rows = await db.execute(
        select(PanelAccount).where(
            PanelAccount.user_id == user.id,
            PanelAccount.panel_id.in_(panel_ids),
        )
    )
    existing = list(existing_rows.scalars().all())
    by_key: dict[tuple[int, int], PanelAccount] = {}
    legacy_by_panel: dict[int, PanelAccount] = {}
    for a in existing:
        gid = getattr(a, "group_id", None)
        if gid is None:
            # Only keep first legacy row per panel
            legacy_by_panel.setdefault(int(a.panel_id), a)
        else:
            by_key[(int(a.panel_id), int(gid))] = a

    panel_counts: dict[int, int] = {}
    for p, _, gid in targets:
        if gid:
            panel_counts[p.id] = panel_counts.get(p.id, 0) + 1

    # Assign legacy merged accounts to the lowest group_id on that panel
    claimed_legacy: set[int] = set()
    for panel, _cfg, group_id in sorted(targets, key=lambda t: (t[0].id, t[2])):
        if not group_id:
            continue
        key = (panel.id, group_id)
        if key in by_key:
            continue
        leg = legacy_by_panel.get(panel.id)
        if leg is not None and leg.id not in claimed_legacy:
            leg.group_id = group_id
            by_key[key] = leg
            claimed_legacy.add(leg.id)

    jobs: list[tuple[PanelAccount, Panel, UserPayload, int]] = []
    created_any = False
    for panel, config, group_id in targets:
        gid = int(group_id or 0)
        key = (panel.id, gid) if gid else (panel.id, 0)
        account = by_key.get(key) if gid else None
        if account is None and not gid:
            account = legacy_by_panel.get(panel.id)
        if account is None:
            account = PanelAccount(
                user_id=user.id,
                panel_id=panel.id,
                group_id=gid or None,
                status=PanelAccountStatus.pending,
            )
            db.add(account)
            if gid:
                by_key[key] = account
            created_any = True
        else:
            account.group_id = gid or account.group_id

        shared = panel_counts.get(panel.id, 0) > 1 and gid > 0
        existing_name = (account.remote_username or "").strip()
        if existing_name:
            preferred = existing_name
        elif shared:
            preferred = make_group_remote_username(user.username, gid)
        else:
            preferred = (user.username or "").strip()

        payload = build_user_payload(
            user,
            uuid=account.uuid,
            panel_config=config,
            remote_username=preferred,
        )
        jobs.append((account, panel, payload, gid))

    if created_any:
        await db.flush()
    # Release SQLite write lock before remote HTTP (can take tens of seconds).
    # expire_on_commit=False keeps account objects usable after commit.
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        pass

    async def _remote(
        panel: Panel, payload: UserPayload, *, prefer_modify: bool
    ) -> AdapterResult:
        adapter = get_adapter(panel)
        t0 = time.monotonic()
        try:
            async with sem:
                if prefer_modify:
                    try:
                        mod = await asyncio.wait_for(
                            adapter.modify_user(payload), timeout=create_timeout
                        )
                        if mod.success:
                            return mod
                        # Missing on remote → fall through to create
                        if not _is_not_found_err(mod.error):
                            # Still try create/upsert path for forks that reject modify
                            pass
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001
                        pass
                return await create_user_with_collision_alias(
                    adapter,
                    payload,
                    astra_username=user.username,
                    panel_id=panel.id,
                    timeout=create_timeout,
                    max_alias_attempts=alias_attempts,
                    probe_timeout=probe_timeout,
                )
        except asyncio.CancelledError:
            # Soft: overall gather cancelled us — heal will finish; not a panel fault
            return _fail_result(_PROVISION_BUDGET_SOFT)
        except Exception as exc:  # noqa: BLE001
            return _fail_result(_exc_msg(exc) or "خطای پنل")
        finally:
            logger.info(
                "provision panel=%s type=%s astra=%s remote=%s dt=%.2fs",
                panel.name,
                getattr(panel.type, "value", panel.type),
                user.username,
                payload.username,
                time.monotonic() - t0,
            )
            try:
                await adapter.close()  # type: ignore[attr-defined]
            except Exception:
                pass

    tasks = [
        asyncio.create_task(
            _remote(
                panel,
                payload,
                prefer_modify=bool((account.remote_username or "").strip())
                and account.status
                in (
                    PanelAccountStatus.active,
                    PanelAccountStatus.error,
                ),
            )
        )
        for account, panel, payload, _ in jobs
    ]
    logger.info(
        "provision start user=%s targets=%s sem=%s budget=%.1fs create_timeout=%.1fs fast=%s",
        user.username,
        n_targets,
        sem_limit,
        budget,
        create_timeout,
        fast,
    )
    done, pending = await asyncio.wait(tasks, timeout=budget)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    results: list[AdapterResult] = []
    for t in tasks:
        if t in pending or t.cancelled():
            results.append(_fail_result(_PROVISION_BUDGET_SOFT))
            continue
        try:
            results.append(t.result())
        except asyncio.CancelledError:
            results.append(_fail_result(_PROVISION_BUDGET_SOFT))
        except Exception as exc:  # noqa: BLE001
            results.append(_fail_result(_exc_msg(exc) or "خطای پنل"))

    accounts: list[PanelAccount] = []
    now = datetime.now(timezone.utc)
    for (account, panel, payload, gid), result in zip(jobs, results):
        coeff = coefficient_from_config(payload.config or {})
        if gid:
            account.group_id = gid
        if result.success:
            account.status = PanelAccountStatus.active
            account.remote_username = (
                result.remote_username or payload.username or user.username
            )
            account.remote_id = result.remote_id or account.remote_id
            account.uuid = result.uuid or account.uuid
            account.links = result.links or account.links or []
            account.links_by_group = None
            account.used_traffic = int(result.used_traffic or 0)
            account.coeff_anchor_raw = 0
            account.coeff_anchor_weighted = 0
            account.applied_coefficient = coeff
            account.error_message = None
            account.last_sync_at = now
            panel.last_ok_at = now
        elif (result.error or "") == _PROVISION_BUDGET_SOFT:
            # Still running when budget hit — keep pending for heal, not fake error
            account.status = PanelAccountStatus.pending
            account.remote_username = (
                account.remote_username or payload.username or user.username
            )
            account.error_message = None
        else:
            account.status = PanelAccountStatus.error
            account.error_message = _format_remote_error(
                panel,
                action="ساخت کاربر",
                err=result.error or "ایجاد در پنل ناموفق",
                username=payload.username,
            )
        accounts.append(account)

    await db.flush()
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        pass
    # Only hydrate when at least one panel succeeded — skip dead-panel waits on create
    if any(a.status == PanelAccountStatus.active for a in accounts):
        try:
            reloaded = await db.execute(
                select(User)
                .where(User.id == user.id)
                .options(
                    selectinload(User.panel_accounts).selectinload(PanelAccount.panel)
                )
            )
            fresh = reloaded.scalar_one_or_none() or user
            await asyncio.wait_for(
                hydrate_share_links(db, fresh), timeout=hydrate_budget
            )
            await db.flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "provision hydrate user=%s: %s", user.username, _exc_msg(exc)
            )

    logger.info(
        "provision done user=%s targets=%s ok=%s err=%s total=%.2fs fast=%s",
        user.username,
        len(jobs),
        sum(1 for a in accounts if a.status == PanelAccountStatus.active),
        sum(1 for a in accounts if a.status == PanelAccountStatus.error),
        time.monotonic() - t_prov,
        fast,
    )
    return accounts



async def reset_user_traffic_on_panels(
    db: AsyncSession,
    user: User,
    *,
    prefer_types: set[PanelType] | None = None,
    remote_timeout: float = 18,
    sync_after: bool = True,
    sync_only_types: set[PanelType] | None = None,
) -> list[dict]:
    """Reset used traffic on every remote panel, then clear local counters.

    Returns per-panel {panel_id, panel_name, ok, error}.
    """
    result = await db.execute(
        select(PanelAccount)
        .where(PanelAccount.user_id == user.id)
        .options(selectinload(PanelAccount.panel))
    )
    accounts = list(result.scalars().all())
    if prefer_types:
        accounts = [
            a
            for a in accounts
            if a.panel is not None and a.panel.type in prefer_types
        ]

    async def _one(account: PanelAccount) -> dict:
        panel = account.panel
        name = panel.name if panel else f"#{account.panel_id}"
        if not panel or not panel.is_active:
            return {
                "panel_id": account.panel_id,
                "panel_name": name,
                "ok": False,
                "error": "پنل غیرفعال",
            }
        if account.status == PanelAccountStatus.deleted:
            return {
                "panel_id": account.panel_id,
                "panel_name": name,
                "ok": True,
                "error": None,
            }
        uname = (account.remote_username or user.username or "").strip()
        rid = account.remote_id or account.remote_username or uname
        # Astra: same-session reset (no SQLite lock / no xray unless enable flips)
        if panel.type == PanelType.astra:
            from app.services import astra_node as astra_node_svc

            try:
                client = await astra_node_svc.reset_client_usage(
                    db,
                    panel,
                    uname,
                    re_enable=True,
                    rebuild=True,
                    sync_nginx=False,
                )
                if not client and rid and str(rid) != uname:
                    client = await astra_node_svc.reset_client_usage(
                        db,
                        panel,
                        str(rid),
                        re_enable=True,
                        rebuild=True,
                        sync_nginx=False,
                    )
                ok = client is not None
                err = None if ok else "user not found"
            except Exception as exc:  # noqa: BLE001
                ok = False
                err = _exc_msg(exc)
            account.used_traffic = 0
            account.coeff_anchor_raw = 0
            account.coeff_anchor_weighted = 0
            if ok:
                account.error_message = None
                account.last_sync_at = datetime.now(timezone.utc)
            return {
                "panel_id": account.panel_id,
                "panel_name": name,
                "ok": ok,
                "error": err,
            }

        adapter = get_adapter(panel)
        try:
            res = await asyncio.wait_for(
                adapter.reset_usage(uname, rid), timeout=remote_timeout
            )
        except Exception as exc:  # noqa: BLE001
            res = _fail_result(_exc_msg(exc))
        if res.success:
            account.used_traffic = 0
            account.coeff_anchor_raw = 0
            account.coeff_anchor_weighted = 0
            account.error_message = None
            account.last_sync_at = datetime.now(timezone.utc)
            return {
                "panel_id": account.panel_id,
                "panel_name": name,
                "ok": True,
                "error": None,
            }
        # Local still resets; report remote failure
        account.used_traffic = 0
        account.coeff_anchor_raw = 0
        account.coeff_anchor_weighted = 0
        return {
            "panel_id": account.panel_id,
            "panel_name": name,
            "ok": False,
            "error": res.error or "بازنشانی روی پنل ناموفق",
        }

    # Astra accounts are sequential (shared session); foreign remotes parallel.
    astra_accounts = [a for a in accounts if a.panel and a.panel.type == PanelType.astra]
    other_accounts = [
        a for a in accounts if not a.panel or a.panel.type != PanelType.astra
    ]
    outcomes: list[dict] = []
    for a in astra_accounts:
        outcomes.append(await _one(a))
    if other_accounts:
        outcomes.extend(await asyncio.gather(*[_one(a) for a in other_accounts]))
    # Always clear Astra-side user totals even if only a subset of panels was reset
    user.lifetime_used_traffic = int(user.lifetime_used_traffic or 0) + int(
        user.used_traffic or 0
    )
    user.used_traffic = 0
    if user.status == UserStatus.limited:
        user.status = UserStatus.active
    # Zero ALL panel accounts locally so weighted usage is 0 immediately
    all_acc_result = await db.execute(
        select(PanelAccount)
        .where(PanelAccount.user_id == user.id)
        .options(selectinload(PanelAccount.panel))
    )
    all_accounts = list(all_acc_result.scalars().all())
    for account in all_accounts:
        if account.status == PanelAccountStatus.deleted:
            continue
        account.used_traffic = 0
        account.coeff_anchor_raw = 0
        account.coeff_anchor_weighted = 0
    await db.flush()
    if sync_after:
        await sync_modify_user(
            db,
            user,
            only_types=sync_only_types,
            remote_timeout=20,
            skip_nginx_sync=True,
        )
        for account in all_accounts:
            if account.status != PanelAccountStatus.deleted:
                account.used_traffic = 0
                account.coeff_anchor_raw = 0
                account.coeff_anchor_weighted = 0
    account_coeffs = await account_coefficients_for_user(db, user, all_accounts)
    user.used_traffic = await _weighted_total(all_accounts, account_coeffs)
    await db.flush()
    return list(outcomes)


async def sync_modify_user_background(
    user_id: int,
    *,
    exclude_types: set[PanelType] | None = None,
    only_types: set[PanelType] | None = None,
) -> None:
    """Best-effort remote sync after a fast Astra-local edit/reset."""
    from app.core.database import AsyncSessionLocal
    from app.services.users import load_user

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.id == user_id))
            row = result.scalar_one_or_none()
            if not row:
                return
            user = await load_user(db, row.username)
            if not user:
                return
            await sync_modify_user(
                db,
                user,
                only_types=only_types,
                exclude_types=exclude_types,
                remote_timeout=20,
                skip_nginx_sync=True,
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("background sync_modify failed user_id=%s", user_id)


async def reset_remotes_background(
    user_id: int, *, exclude_types: set[PanelType] | None = None
) -> None:
    """Reset + re-push quota on foreign remotes after a fast Astra reset."""
    from app.core.database import AsyncSessionLocal
    from app.services.users import load_user

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.id == user_id))
            row = result.scalar_one_or_none()
            if not row:
                return
            user = await load_user(db, row.username)
            if not user:
                return
            # Reset only non-excluded panels, then push caps
            result = await db.execute(
                select(PanelAccount)
                .where(PanelAccount.user_id == user.id)
                .options(selectinload(PanelAccount.panel))
            )
            accounts = list(result.scalars().all())
            targets = [
                a
                for a in accounts
                if a.panel
                and a.panel.is_active
                and a.status != PanelAccountStatus.deleted
                and (not exclude_types or a.panel.type not in exclude_types)
            ]

            async def _one(account: PanelAccount) -> None:
                panel = account.panel
                if not panel:
                    return
                adapter = get_adapter(panel)
                uname = (account.remote_username or user.username or "").strip()
                rid = account.remote_id or account.remote_username or uname
                try:
                    await asyncio.wait_for(
                        adapter.reset_usage(uname, rid), timeout=12
                    )
                except Exception:  # noqa: BLE001
                    pass
                account.used_traffic = 0

            if targets:
                await asyncio.gather(*[_one(a) for a in targets])
            await sync_modify_user(
                db,
                user,
                exclude_types=exclude_types,
                remote_timeout=20,
                skip_nginx_sync=True,
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("background reset remotes failed user_id=%s", user_id)


async def sync_modify_user(
    db: AsyncSession,
    user: User,
    *,
    only_types: set[PanelType] | None = None,
    exclude_types: set[PanelType] | None = None,
    remote_timeout: float = 25,
    skip_nginx_sync: bool = False,
) -> None:
    """Push status + shared remaining quota to every remote panel account.

    When Astra is limited/expired/disabled, every panel type (Marzban / PasarGuard /
    Sanaei / Rebecca) must receive the freeze — with retries on flaky links.
    """
    result = await db.execute(
        select(PanelAccount)
        .where(PanelAccount.user_id == user.id)
        .options(selectinload(PanelAccount.panel))
    )
    accounts = list(result.scalars().all())

    ug = await db.execute(select(UserGroup.group_id).where(UserGroup.user_id == user.id))
    group_ids = [row[0] for row in ug.all()]
    claim_cfg: dict[tuple[int, int], dict] = {}
    panel_ids_in_groups: set[int] = set()
    if group_ids:
        for panel, cfg, gid in await resolve_panels_for_groups(db, group_ids):
            claim_cfg[(int(panel.id), int(gid))] = dict(cfg or {})
            panel_ids_in_groups.add(int(panel.id))

    account_coeffs = await account_coefficients_for_user(db, user, accounts)
    weighted = await _weighted_total(accounts, account_coeffs)
    user.used_traffic = weighted

    blocked = user.status in (
        UserStatus.limited,
        UserStatus.expired,
        UserStatus.disabled,
    )

    jobs: list[tuple[PanelAccount, Panel, UserPayload]] = []
    for account in accounts:
        panel = account.panel
        if not panel or not panel.is_active:
            continue
        if account.status == PanelAccountStatus.deleted:
            continue
        gid = int(getattr(account, "group_id", 0) or 0)
        claim_key = (int(account.panel_id), gid) if gid else None
        in_groups = (
            (claim_key in claim_cfg)
            if claim_key
            else (int(account.panel_id) in panel_ids_in_groups)
        )
        # Skip orphan panel rows (no longer in user's groups) unless freezing all
        if not in_groups and not blocked:
            if account.status == PanelAccountStatus.error and not (account.links or []):
                account.status = PanelAccountStatus.deleted
                account.error_message = None
            continue
        # Still push disable/limits to error accounts — they often exist on remote
        if claim_key and claim_key in claim_cfg:
            cfg = dict(claim_cfg[claim_key])
        else:
            # Legacy NULL group_id: pick any claim config for this panel
            cfg = next(
                (dict(c) for (pid, _g), c in claim_cfg.items() if pid == account.panel_id),
                {},
            )
        if only_types is not None and panel.type not in only_types:
            continue
        if exclude_types is not None and panel.type in exclude_types:
            continue
        rem_name = (account.remote_username or user.username or "").strip()
        payload = build_user_payload(
            user,
            uuid=account.uuid,
            panel_config=cfg,
            used_weighted_total=weighted,
            used_raw_on_panel=int(account.used_traffic or 0),
            remote_username=rem_name,
        )
        # PasarGuard disable API needs numeric by-id — pass through stored remote_id
        rid = (account.remote_id or "").strip()
        if rid:
            payload.config = {**(payload.config or {}), "_remote_id": rid}
        # Quota-only Astra upserts: skip nginx; xray reload only if enable flips
        if panel.type == PanelType.astra and skip_nginx_sync:
            payload.config = {
                **(payload.config or {}),
                "_astra_skip_nginx": True,
            }
        jobs.append((account, panel, payload))

    sem = _remote_sem()

    async def _remote(
        panel: Panel,
        payload: UserPayload,
        account: PanelAccount,
        *,
        timeout: float = 25,
    ) -> AdapterResult:
        # Freeze pushes get extra retries — soft-fail left remotes online before
        attempts = 3 if blocked else 1
        last = _fail_result("modify failed")
        for i in range(attempts):
            adapter = get_adapter(panel)
            try:
                async with sem:
                    last = await asyncio.wait_for(
                        adapter.modify_user(payload),
                        timeout=timeout if not blocked else max(timeout, 14),
                    )
                if last.success:
                    return last
                # Missing / reserved name → create with collision aliases
                if _is_not_found_err(last.error) or _is_username_taken_err(last.error):
                    created = await create_user_with_collision_alias(
                        adapter,
                        payload,
                        astra_username=user.username,
                        panel_id=panel.id,
                        timeout=min(30.0, timeout + 5),
                    )
                    if created.success:
                        return created
                    last = created
            except Exception as e:  # noqa: BLE001
                last = _fail_result(_exc_msg(e))
            finally:
                try:
                    await adapter.close()  # type: ignore[attr-defined]
                except Exception:
                    pass
            if i + 1 < attempts:
                await asyncio.sleep(0.2)
        return last

    async def _astra_inline(
        panel: Panel, payload: UserPayload
    ) -> AdapterResult:
        """Apply Astra upsert on the SAME db session (avoids SQLite lock/timeout)."""
        from app.services import astra_node as astra_node_svc

        try:
            enable = (payload.status or "active").lower() in ("active", "on_hold")
            limit_ip = 0
            cfg = payload.config or {}
            try:
                limit_ip = max(0, int(cfg.get("limit_ip", cfg.get("ip_limit", 0)) or 0))
            except (TypeError, ValueError):
                limit_ip = 0
            client = await astra_node_svc.upsert_client(
                db,
                panel,
                email=payload.username,
                uuid=payload.uuid,
                data_limit=int(payload.data_limit or 0),
                expire=int(payload.expire or 0),
                enable=enable,
                limit_ip=limit_ip,
                rebuild=True,
                sync_nginx=False,
            )
            used = int(client.used_up or 0) + int(client.used_down or 0)
            # Soft quota without full client_usage_and_links (no extra rebuilds)
            if (
                enable
                and int(payload.data_limit or 0) > 0
                and used >= int(payload.data_limit or 0)
                and client.enable
            ):
                client.enable = False
                await db.flush()
                await astra_node_svc.rebuild_xray_config(
                    db, panel, sync_nginx=False
                )
            return AdapterResult(
                success=True,
                remote_username=payload.username,
                remote_id=str(client.id),
                uuid=client.uuid,
                links=[],
                used_traffic=used,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("astra inline modify failed panel=%s", panel.id)
            return _fail_result(_exc_msg(e))

    if not jobs:
        await db.flush()
        return

    astra_jobs = [j for j in jobs if j[1].type == PanelType.astra]
    other_jobs = [j for j in jobs if j[1].type != PanelType.astra]

    results_by_key: dict[tuple[int, int], AdapterResult] = {}

    # Foreign remotes in parallel (HTTP). Astra sequentially on shared session.
    if other_jobs:
        other_res = await asyncio.gather(
            *[
                _remote(panel, payload, account, timeout=remote_timeout)
                for account, panel, payload in other_jobs
            ]
        )
        for (account, panel, payload), res in zip(other_jobs, other_res):
            results_by_key[(int(account.panel_id), int(account.id))] = res

    for account, panel, payload in astra_jobs:
        res = await _astra_inline(panel, payload)
        results_by_key[(int(account.panel_id), int(account.id))] = res

    if astra_jobs and not skip_nginx_sync:
        try:
            from app.services import astra_node as astra_node_svc

            await astra_node_svc.sync_nginx_ws_upstreams(db)
        except Exception:  # noqa: BLE001
            logger.debug("astra nginx sync after modify failed", exc_info=True)

    results = [
        results_by_key[(int(account.panel_id), int(account.id))]
        for account, panel, payload in jobs
    ]
    now = datetime.now(timezone.utc)
    for (account, panel, payload), res in zip(jobs, results):
        if res.success:
            account.status = PanelAccountStatus.active
            account.remote_username = (
                res.remote_username
                or payload.username
                or account.remote_username
                or user.username
            )
            account.remote_id = res.remote_id or account.remote_id
            if res.uuid:
                account.uuid = res.uuid
            if res.links:
                account.links = res.links
            # Only update raw usage when panel returned a real reading
            if res.used_traffic is not None and int(res.used_traffic) >= 0:
                prev = int(account.used_traffic or 0)
                got = int(res.used_traffic or 0)
                # Astra node is authoritative (supports real resets to 0).
                # Other remotes: never wipe a positive raw with a flaky 0.
                if panel and panel.type == PanelType.astra:
                    account.used_traffic = got
                else:
                    account.used_traffic = (
                        max(prev, got) if prev > 0 and got == 0 else got
                    )
            account.error_message = None
            account.last_sync_at = now
            if panel:
                panel.last_ok_at = now
        else:
            # Soft: keep active + links if we already have share configs and
            # failure is not a hard "missing user" after create attempt.
            had_links = bool(account.links)
            was_ok = account.status == PanelAccountStatus.active and had_links
            msg = _format_remote_error(
                panel,
                action="به‌روزرسانی / ساخت",
                err=res.error or "به‌روزرسانی پنل ناموفق",
                username=payload.username,
            )
            if blocked:
                # Quota freeze MUST NOT soft-keep active — that left remotes
                # online until an admin opened Astra. Mark error so the next
                # watch cycle retries hard.
                account.status = PanelAccountStatus.error
                account.error_message = msg
                logger.warning(
                    "failed to freeze remote user=%s panel=%s: %s",
                    user.username,
                    account.panel_id,
                    msg,
                )
            elif was_ok and not _is_not_found_err(res.error):
                # Transient modify fail — keep active, store detail for admin
                account.error_message = None
                logger.warning(
                    "soft modify fail kept active user=%s panel=%s: %s",
                    user.username,
                    account.panel_id,
                    msg,
                )
            else:
                account.status = PanelAccountStatus.error
                account.error_message = msg
    await db.flush()
    # Recompute weighted usage after any raw changes (existing panels unchanged)
    account_coeffs = await account_coefficients_for_user(db, user, accounts)
    user.used_traffic = await _weighted_total(accounts, account_coeffs)
    await db.flush()


async def sync_delete_user(db: AsyncSession, user: User) -> list[dict]:
    """Best-effort delete on remotes, then always allow Astra user removal.

    ok=True outcomes (caller may delete Astra user for all of these):
      - remote DELETE succeeded
      - remote already gone (404 / not found)
      - panel missing / inactive / removed from Astra
      - empty remote username (nothing to call)
      - unreachable / timeout / connection errors (panel offline)

    A dead or removed panel must never block deleting the Astra user.
    Per-panel attempts are short; overall budget is capped.
    """
    result = await db.execute(
        select(PanelAccount)
        .where(PanelAccount.user_id == user.id)
        .options(selectinload(PanelAccount.panel))
    )
    accounts = list(result.scalars().all())
    if not accounts:
        return []

    sem = _remote_sem()
    t0 = time.monotonic()
    per_panel_timeout = 6.0
    overall_budget = min(18.0, 4.0 + 6.0 * max(1, len(accounts)))

    async def _remote(account: PanelAccount) -> dict:
        panel = account.panel
        name = panel.name if panel else f"#{account.panel_id}"
        uname = (account.remote_username or user.username or "").strip()
        base = {
            "panel_id": account.panel_id,
            "panel_name": name,
            "account_id": account.id,
        }

        # Panel gone from Astra / turned off → skip remote, treat as cleaned
        if not panel:
            return {
                **base,
                "ok": True,
                "skipped": True,
                "error": "پنل دیگر در Astra نیست — رد شد",
            }
        if not bool(getattr(panel, "is_active", True)):
            return {
                **base,
                "ok": True,
                "skipped": True,
                "error": "پنل خاموش است — رد شد",
            }
        if not uname:
            return {
                **base,
                "ok": True,
                "skipped": True,
                "error": "یوزرنیم ریموت خالی — رد شد",
            }

        adapter = get_adapter(panel)
        try:
            async with sem:
                res = await asyncio.wait_for(
                    adapter.delete_user(
                        uname, account.remote_id or account.remote_username
                    ),
                    timeout=per_panel_timeout,
                )
        except asyncio.CancelledError:
            return {
                **base,
                "ok": True,
                "skipped": True,
                "error": "زمان حذف پنل تمام شد — رد شد",
            }
        except Exception as e:  # noqa: BLE001
            msg = _exc_msg(e)
            # Unreachable / timeout → do not block Astra delete
            if _is_unreachable_err(msg) or _is_not_found_err(msg):
                return {
                    **base,
                    "ok": True,
                    "skipped": True,
                    "error": msg or "پنل در دسترس نیست — رد شد",
                }
            return {
                **base,
                "ok": True,
                "skipped": True,
                "error": msg or "خطای پنل — رد شد تا حذف Astra انجام شود",
            }
        finally:
            try:
                await adapter.close()  # type: ignore[attr-defined]
            except Exception:
                pass

        if res.success or _is_not_found_err(res.error):
            return {**base, "ok": True, "skipped": False, "error": None}
        # Panel answered but delete failed — still allow Astra cleanup; note it
        if _is_unreachable_err(res.error):
            return {
                **base,
                "ok": True,
                "skipped": True,
                "error": res.error or "پنل در دسترس نیست — رد شد",
            }
        return {
            **base,
            "ok": True,
            "skipped": True,
            "error": res.error or "حذف ریموت ناموفق — رد شد",
        }

    tasks = [asyncio.create_task(_remote(a)) for a in accounts]
    done, pending = await asyncio.wait(tasks, timeout=overall_budget)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    outcomes: list[dict] = []
    for t, account in zip(tasks, accounts):
        if t in pending or t.cancelled():
            outcomes.append(
                {
                    "panel_id": account.panel_id,
                    "panel_name": account.panel.name
                    if account.panel
                    else f"#{account.panel_id}",
                    "account_id": account.id,
                    "ok": True,
                    "skipped": True,
                    "error": "زمان کلی حذف تمام شد — رد شد",
                }
            )
            continue
        try:
            outcomes.append(t.result())
        except Exception as exc:  # noqa: BLE001
            outcomes.append(
                {
                    "panel_id": account.panel_id,
                    "panel_name": account.panel.name
                    if account.panel
                    else f"#{account.panel_id}",
                    "account_id": account.id,
                    "ok": True,
                    "skipped": True,
                    "error": _exc_msg(exc) or "خطا — رد شد",
                }
            )

    # Always mark accounts deleted locally — Astra user is going away
    by_id = {a.id: a for a in accounts}
    for row in outcomes:
        account = by_id.get(row.get("account_id"))
        if account is None:
            continue
        account.status = PanelAccountStatus.deleted
        account.error_message = None

    await db.flush()
    logger.info(
        "delete done user=%s panels=%s remote_ok=%s skipped=%s total=%.2fs",
        user.username,
        len(accounts),
        sum(1 for r in outcomes if r.get("ok") and not r.get("skipped")),
        sum(1 for r in outcomes if r.get("skipped")),
        time.monotonic() - t0,
    )
    return outcomes


async def _delete_panel_accounts_remote(
    db: AsyncSession,
    user: User,
    accounts: list[PanelAccount],
    *,
    clear_links_always: bool = True,
) -> list[dict]:
    """Delete the given PanelAccount rows on remotes (all panel types) and mark deleted.

    Retries once per account. Treats not-found as success. When
    ``clear_links_always`` is True, share links are wiped even if remote delete
    fails so old/new subs cannot keep emitting those configs.
    """
    if not accounts:
        return []

    sem = _remote_sem()
    t0 = time.monotonic()

    async def _remote(account: PanelAccount) -> dict:
        panel = account.panel
        name = panel.name if panel else f"#{account.panel_id}"
        uname = (account.remote_username or user.username or "").strip()
        base = {
            "panel_id": account.panel_id,
            "panel_name": name,
            "account_id": account.id,
            "group_id": int(account.group_id) if account.group_id is not None else None,
        }

        if not panel:
            return {**base, "ok": False, "error": "پنل پیدا نشد — حذف لغو شد"}
        if not uname:
            return {**base, "ok": False, "error": "یوزرنیم ریموت خالی است"}

        # Inactive Astra panels: still try delete so remotes don't keep junk
        last_err = "حذف از پنل ناموفق بود"
        for attempt in range(2):
            adapter = get_adapter(panel)
            try:
                async with sem:
                    res = await asyncio.wait_for(
                        adapter.delete_user(
                            uname, account.remote_id or account.remote_username
                        ),
                        timeout=16,
                    )
            except Exception as e:  # noqa: BLE001
                last_err = _exc_msg(e)
                res = None
            finally:
                try:
                    await adapter.close()  # type: ignore[attr-defined]
                except Exception:
                    pass

            if res is not None and (res.success or _is_not_found_err(res.error)):
                return {**base, "ok": True, "error": None}
            if res is not None:
                last_err = res.error or last_err
            if attempt == 0:
                await asyncio.sleep(0.35)

        return {**base, "ok": False, "error": last_err}

    outcomes = list(await asyncio.gather(*[_remote(a) for a in accounts]))

    by_id = {a.id: a for a in accounts}
    for row in outcomes:
        account = by_id.get(row.get("account_id"))
        if account is None:
            continue
        if clear_links_always:
            account.links = []
            account.links_by_group = {}
        if row.get("ok"):
            account.status = PanelAccountStatus.deleted
            account.error_message = None
            account.links = []
            account.links_by_group = {}
        else:
            account.error_message = row.get("error") or "حذف از پنل ناموفق بود"

    await db.flush()
    logger.info(
        "delete-panel-accounts user=%s panels=%s ok=%s total=%.2fs",
        user.username,
        len(accounts),
        sum(1 for r in outcomes if r.get("ok")),
        time.monotonic() - t0,
    )
    return outcomes


async def deprovision_all_accounts_on_panel(db: AsyncSession, panel_id: int) -> dict:
    """Delete every remote account on this panel, then mark local rows deleted.

    Used before removing a panel from Astra so remotes are not left with live configs.
    """
    pid = int(panel_id)
    result = await db.execute(
        select(PanelAccount)
        .where(
            PanelAccount.panel_id == pid,
            PanelAccount.status != PanelAccountStatus.deleted,
        )
        .options(
            selectinload(PanelAccount.panel),
            selectinload(PanelAccount.user),
        )
    )
    accounts = list(result.scalars().unique().all())
    if not accounts:
        return {"accounts": 0, "ok": 0, "fail": 0}

    by_user: dict[int, list[PanelAccount]] = {}
    users: dict[int, User] = {}
    for a in accounts:
        uid = int(a.user_id)
        by_user.setdefault(uid, []).append(a)
        if a.user is not None:
            users[uid] = a.user

    ok = 0
    fail = 0
    for uid, accs in by_user.items():
        user = users.get(uid)
        if user is None:
            user = await db.get(User, uid)
        if user is None:
            for a in accs:
                a.links = []
                a.links_by_group = {}
                a.status = PanelAccountStatus.deleted
            fail += len(accs)
            continue
        outcomes = await _delete_panel_accounts_remote(db, user, accs)
        ok += sum(1 for r in outcomes if r.get("ok"))
        fail += sum(1 for r in outcomes if not r.get("ok"))
        # Force local gone even if remote failed — panel row is about to be deleted
        for a in accs:
            a.status = PanelAccountStatus.deleted
            a.links = []
            a.links_by_group = {}

    await db.flush()
    logger.info(
        "deprovision-panel panel=%s accounts=%s ok=%s fail=%s",
        pid,
        len(accounts),
        ok,
        fail,
    )
    return {"accounts": len(accounts), "ok": ok, "fail": fail}


async def sync_delete_user_group_accounts(
    db: AsyncSession,
    user: User,
    group_ids: list[int],
) -> list[dict]:
    """
    Delete remote accounts that belong to the given Astra groups, then mark
    those PanelAccount rows deleted.

    Used when an admin removes groups from a user. Caller must only update
    UserGroup after every outcome is ok=True (same contract as full user delete).
    """
    want = {int(g) for g in group_ids if g is not None}
    if not want:
        return []

    result = await db.execute(
        select(PanelAccount)
        .where(PanelAccount.user_id == user.id)
        .options(selectinload(PanelAccount.panel))
    )
    accounts = [
        a
        for a in result.scalars().all()
        if a.status != PanelAccountStatus.deleted
        and getattr(a, "group_id", None) is not None
        and int(a.group_id) in want
    ]
    return await _delete_panel_accounts_remote(db, user, accounts)


async def sync_delete_user_group_panel_accounts(
    db: AsyncSession,
    user: User,
    group_id: int,
    panel_ids: list[int],
) -> list[dict]:
    """
    Delete remotes for specific (panel × group) accounts.

    Used when an admin removes panels from a sales group — configs must leave
    both the subscription feed and the upstream panel, not merely hide in sub.
    """
    want_panels = {int(p) for p in panel_ids if p is not None}
    gid = int(group_id)
    if not want_panels:
        return []

    result = await db.execute(
        select(PanelAccount)
        .where(PanelAccount.user_id == user.id)
        .options(selectinload(PanelAccount.panel))
    )
    accounts = [
        a
        for a in result.scalars().all()
        if a.status != PanelAccountStatus.deleted
        and int(a.panel_id) in want_panels
        and getattr(a, "group_id", None) is not None
        and int(a.group_id) == gid
    ]
    return await _delete_panel_accounts_remote(db, user, accounts)


async def retry_failed_accounts(db: AsyncSession, user: User) -> list[PanelAccount]:
    """Retry error/pending on live panels; drop rows for off/missing panels.

    A disabled panel must not keep showing as a user error — those local rows
    are marked deleted (no remote call; panel is already unreachable/off).
    """
    result = await db.execute(
        select(PanelAccount)
        .where(
            PanelAccount.user_id == user.id,
            PanelAccount.status.in_(
                (PanelAccountStatus.error, PanelAccountStatus.pending)
            ),
        )
        .options(selectinload(PanelAccount.panel))
    )
    failed = list(result.scalars().all())
    if not failed:
        return []

    ug = await db.execute(select(UserGroup.group_id).where(UserGroup.user_id == user.id))
    group_ids = [row[0] for row in ug.all()]
    claim_cfg: dict[tuple[int, int], dict] = {}
    if group_ids:
        for panel, cfg, gid in await resolve_panels_for_groups(db, group_ids):
            claim_cfg[(int(panel.id), int(gid))] = dict(cfg or {})

    panels: list[tuple[Panel, dict, int]] = []
    dropped = 0
    for a in failed:
        panel = a.panel
        if panel is None or not bool(getattr(panel, "is_active", True)):
            a.status = PanelAccountStatus.deleted
            a.error_message = None
            a.links = []
            a.links_by_group = {}
            dropped += 1
            continue
        gid = int(getattr(a, "group_id", 0) or 0)
        cfg = claim_cfg.get((int(a.panel_id), gid)) or next(
            (c for (pid, _g), c in claim_cfg.items() if pid == a.panel_id),
            {},
        )
        panels.append((panel, dict(cfg), gid))

    if dropped:
        await db.flush()
        logger.info(
            "retry drop inactive-panel errors user=%s dropped=%s",
            user.username,
            dropped,
        )

    if not panels:
        return []
    return await provision_user_on_panels(
        db, user, panels, fast=True, overall_timeout=35.0
    )


def _empty_retry_all_stats() -> dict:
    return {
        "attempted": 0,
        "recovered": 0,
        "remaining": 0,
        "dropped": 0,
        "skipped_unreachable": 0,
        "panels_ok": [],
        "panels_down": [],
        "users_recovered": 0,
        "users_remaining": 0,
    }


async def retry_all_failed_accounts(
    db: AsyncSession,
    *,
    overall_timeout: float = 180.0,
    per_remote_timeout: float = 22.0,
    probe_timeout: float = 12.0,
    concurrency: int = 20,
    limit_accounts: int | None = None,
) -> dict:
    """Retry every error/pending account across live panels.

    Probe is soft: only hard-down panels are skipped. Timeouts/cancels verify
    with ``get_usage`` so already-provisioned remotes clear sticky false errors.
    """
    async with _bulk_retry_lock:
        return await _retry_all_failed_accounts_inner(
            db,
            overall_timeout=overall_timeout,
            per_remote_timeout=per_remote_timeout,
            probe_timeout=probe_timeout,
            concurrency=concurrency,
            limit_accounts=limit_accounts,
        )


async def _probe_panel_alive(panel: Panel, timeout: float) -> tuple[str, str]:
    """Return (state, err) where state is ``ok`` | ``soft`` | ``down``."""
    adapter = get_adapter(panel)
    try:
        result = await asyncio.wait_for(adapter.test_connection(), timeout=timeout)
        if result.success:
            return "ok", ""
        err = (result.error or "probe failed").strip() or "probe failed"
        if _is_hard_down_err(err):
            return "down", err
        # Ambiguous failure (timeout/ssl/5xx) — still try account ops
        return "soft", err
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        err = _exc_msg(exc) or "Timeout waiting for panel response"
        if _is_hard_down_err(err):
            return "down", err
        return "soft", err
    finally:
        try:
            await adapter.close()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


async def _retry_all_failed_accounts_inner(
    db: AsyncSession,
    *,
    overall_timeout: float,
    per_remote_timeout: float,
    probe_timeout: float,
    concurrency: int,
    limit_accounts: int | None,
) -> dict:
    t0 = time.monotonic()
    stats = _empty_retry_all_stats()
    result = await db.execute(
        select(PanelAccount)
        .where(
            PanelAccount.status.in_(
                (PanelAccountStatus.error, PanelAccountStatus.pending)
            )
        )
        .options(
            selectinload(PanelAccount.panel),
            selectinload(PanelAccount.user),
        )
        .order_by(PanelAccount.id.asc())
    )
    failed = list(result.scalars().all())
    if not failed:
        return stats
    start_error_users = {int(a.user_id) for a in failed}

    live: list[PanelAccount] = []
    dropped = 0
    for a in failed:
        panel = a.panel
        if panel is None or not bool(getattr(panel, "is_active", True)):
            a.status = PanelAccountStatus.deleted
            a.error_message = None
            a.links = []
            a.links_by_group = {}
            dropped += 1
            continue
        live.append(a)
    stats["dropped"] = dropped
    if dropped:
        await db.flush()
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            pass
        logger.info("retry-all drop inactive-panel errors dropped=%s", dropped)

    if limit_accounts is not None and limit_accounts > 0:
        live = live[: int(limit_accounts)]
    if not live:
        stats["users_recovered"] = len(start_error_users)
        return stats

    panels_by_id: dict[int, Panel] = {}
    for a in live:
        if a.panel is not None:
            panels_by_id[int(a.panel_id)] = a.panel

    probe_tasks = {
        pid: asyncio.create_task(_probe_panel_alive(panel, probe_timeout))
        for pid, panel in panels_by_id.items()
    }
    probe_state: dict[int, str] = {}
    if probe_tasks:
        _done, probe_pending = await asyncio.wait(
            probe_tasks.values(), timeout=float(probe_timeout) + 3.0
        )
        for t in probe_pending:
            t.cancel()
        if probe_pending:
            await asyncio.gather(*probe_pending, return_exceptions=True)
        for pid, task in probe_tasks.items():
            if task in probe_pending or task.cancelled():
                # Timed out probe → soft (still attempt accounts)
                probe_state[pid] = "soft"
                continue
            try:
                state, _err = task.result()
                probe_state[pid] = state if state in ("ok", "soft", "down") else "soft"
            except Exception:  # noqa: BLE001
                probe_state[pid] = "soft"

    panels_ok_names: list[str] = []
    panels_down_names: list[str] = []
    reachable: list[PanelAccount] = []
    skipped = 0
    for a in live:
        pid = int(a.panel_id)
        name = (a.panel.name if a.panel else None) or f"#{pid}"
        state = probe_state.get(pid, "soft")
        if state == "down":
            skipped += 1
            if name not in panels_down_names:
                panels_down_names.append(name)
            continue
        reachable.append(a)
        if name not in panels_ok_names:
            panels_ok_names.append(name)

    stats["skipped_unreachable"] = skipped
    stats["panels_ok"] = panels_ok_names
    stats["panels_down"] = panels_down_names
    stats["attempted"] = len(reachable)

    if not reachable:
        still_error_rows = [
            a
            for a in failed
            if a.status
            in (PanelAccountStatus.error, PanelAccountStatus.pending)
        ]
        end_error_users = {int(a.user_id) for a in still_error_rows}
        stats["remaining"] = len(still_error_rows)
        stats["users_remaining"] = len(end_error_users)
        stats["users_recovered"] = len(start_error_users - end_error_users)
        logger.info(
            "retry-all skip all hard-down accounts=%s panels=%s dt=%.2fs",
            skipped,
            ",".join(panels_down_names) or "-",
            time.monotonic() - t0,
        )
        return stats

    user_ids = {int(a.user_id) for a in reachable}
    ug = await db.execute(
        select(UserGroup.user_id, UserGroup.group_id).where(
            UserGroup.user_id.in_(user_ids or [-1])
        )
    )
    user_gids: dict[int, set[int]] = {}
    all_gids: set[int] = set()
    for uid, gid in ug.all():
        user_gids.setdefault(int(uid), set()).add(int(gid))
        all_gids.add(int(gid))

    claim_cfg: dict[tuple[int, int], dict] = {}
    if all_gids:
        for panel, cfg, gid in await resolve_panels_for_groups(db, list(all_gids)):
            claim_cfg[(int(panel.id), int(gid))] = dict(cfg or {})

    user_panel_n: dict[tuple[int, int], int] = {}
    for uid, gids in user_gids.items():
        for (pid, gid), _cfg in claim_cfg.items():
            if gid in gids:
                user_panel_n[(uid, pid)] = user_panel_n.get((uid, pid), 0) + 1

    jobs: list[tuple[PanelAccount, Panel, User, UserPayload]] = []
    for a in reachable:
        user = a.user
        panel = a.panel
        if user is None or panel is None:
            skipped += 1
            continue
        gid = int(getattr(a, "group_id", 0) or 0)
        cfg = claim_cfg.get((int(a.panel_id), gid)) or next(
            (c for (pid, _g), c in claim_cfg.items() if pid == a.panel_id),
            {},
        )
        shared = user_panel_n.get((int(user.id), int(panel.id)), 0) > 1 and gid > 0
        existing_name = (a.remote_username or "").strip()
        if existing_name:
            preferred = existing_name
        elif shared:
            preferred = make_group_remote_username(user.username, gid)
        else:
            preferred = (user.username or "").strip()
        payload = build_user_payload(
            user,
            uuid=a.uuid,
            panel_config=cfg,
            remote_username=preferred,
        )
        jobs.append((a, panel, user, payload))

    stats["attempted"] = len(jobs)
    stats["skipped_unreachable"] = skipped
    if not jobs:
        still_error_rows = [
            a
            for a in failed
            if a.status
            in (PanelAccountStatus.error, PanelAccountStatus.pending)
        ]
        end_error_users = {int(a.user_id) for a in still_error_rows}
        stats["remaining"] = len(still_error_rows)
        stats["users_remaining"] = len(end_error_users)
        stats["users_recovered"] = len(start_error_users - end_error_users)
        return stats

    adapters: dict[int, object] = {}
    for _a, panel, _u, _p in jobs:
        pid = int(panel.id)
        if pid not in adapters:
            adapters[pid] = get_adapter(panel)

    sem = asyncio.Semaphore(max(4, min(int(concurrency), 32)))
    create_timeout = float(per_remote_timeout)
    verify_timeout = min(8.0, max(4.0, create_timeout * 0.35))

    async def _remote(
        account: PanelAccount, panel: Panel, payload: UserPayload
    ) -> AdapterResult:
        adapter = adapters[int(panel.id)]
        prefer_modify = bool((account.remote_username or "").strip())
        try:
            async with sem:
                if prefer_modify:
                    try:
                        mod = await asyncio.wait_for(
                            adapter.modify_user(payload),  # type: ignore[attr-defined]
                            timeout=create_timeout,
                        )
                        if mod.success:
                            return mod
                        # Hard network refuse — don't spam create
                        if (
                            not _is_not_found_err(mod.error)
                            and _is_hard_down_err(mod.error)
                        ):
                            return mod
                        # Soft unreachable / not-found → fall through to verify+create
                    except asyncio.CancelledError:
                        raise
                    except asyncio.TimeoutError:
                        # Modify may have applied — verify before create
                        verified = await _verify_remote_user_exists(
                            adapter,
                            payload.username,
                            account.remote_id,
                            timeout=verify_timeout,
                        )
                        if verified:
                            return verified
                    except Exception:  # noqa: BLE001
                        pass

                # Already exists? Clear sticky error without create.
                verified = await _verify_remote_user_exists(
                    adapter,
                    payload.username,
                    account.remote_id,
                    timeout=verify_timeout,
                )
                if verified:
                    return verified

                return await create_user_with_collision_alias(
                    adapter,
                    payload,
                    astra_username=account.user.username
                    if account.user
                    else payload.username,
                    panel_id=panel.id,
                    timeout=create_timeout,
                    max_alias_attempts=1,
                    probe_timeout=min(6.0, verify_timeout),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Last chance: maybe remote already has the user
            try:
                verified = await _verify_remote_user_exists(
                    adapter,
                    payload.username,
                    account.remote_id,
                    timeout=verify_timeout,
                )
                if verified:
                    return verified
            except Exception:  # noqa: BLE001
                pass
            return _fail_result(_exc_msg(exc) or "خطای پنل")

    # Scale wall budget with job count so large error sets don't cancel mid-flight
    waves = max(1, (len(jobs) + max(1, concurrency) - 1) // max(1, concurrency))
    budget = min(
        300.0,
        max(float(overall_timeout), 20.0 + waves * float(per_remote_timeout)),
    )

    tasks = [
        asyncio.create_task(_remote(account, panel, payload))
        for account, panel, _user, payload in jobs
    ]
    _done, pending = await asyncio.wait(tasks, timeout=budget)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    now = datetime.now(timezone.utc)
    recovered = 0

    async def _apply_success(
        account: PanelAccount,
        panel: Panel,
        user: User,
        payload: UserPayload,
        remote: AdapterResult,
    ) -> None:
        nonlocal recovered
        account.status = PanelAccountStatus.active
        account.remote_username = (
            remote.remote_username or payload.username or user.username
        )
        account.remote_id = remote.remote_id or account.remote_id
        account.uuid = remote.uuid or account.uuid
        if remote.links:
            account.links = remote.links
        account.error_message = None
        account.last_sync_at = now
        panel.last_ok_at = now
        recovered += 1

    for (account, panel, user, payload), task in zip(jobs, tasks):
        if task in pending or task.cancelled():
            # Budget cut — verify existence before leaving a sticky error
            adapter = adapters.get(int(panel.id))
            verified = None
            if adapter is not None:
                verified = await _verify_remote_user_exists(
                    adapter,
                    payload.username,
                    account.remote_id,
                    timeout=verify_timeout,
                )
            if verified and verified.success:
                await _apply_success(account, panel, user, payload, verified)
            else:
                # Soft pending: heal loop will finish; don't keep fake hard error
                account.status = PanelAccountStatus.pending
                account.error_message = None
            continue
        try:
            remote = task.result()
        except asyncio.CancelledError:
            account.status = PanelAccountStatus.pending
            account.error_message = None
            continue
        except Exception as exc:  # noqa: BLE001
            remote = _fail_result(_exc_msg(exc) or "خطای پنل")

        if remote.success:
            await _apply_success(account, panel, user, payload, remote)
        elif _is_hard_down_err(remote.error):
            account.status = PanelAccountStatus.error
            account.error_message = _format_remote_error(
                panel,
                action="به‌روزرسانی / ساخت",
                err=remote.error or "پنل در دسترس نیست",
                username=payload.username,
            )
        elif _is_unreachable_err(remote.error):
            # Soft / transient — pending for heal, not sticky banner error
            account.status = PanelAccountStatus.pending
            account.error_message = None
        else:
            account.status = PanelAccountStatus.error
            account.error_message = _format_remote_error(
                panel,
                action="به‌روزرسانی / ساخت",
                err=remote.error or "ایجاد در پنل ناموفق",
                username=payload.username,
            )

    try:
        await db.flush()
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("retry-all flush/commit failed: %s", _exc_msg(exc))
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass

    for adapter in adapters.values():
        try:
            await adapter.close()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    # Banner counts only hard errors (pending is soft / healable)
    still_error_rows = [
        a for a in failed if a.status == PanelAccountStatus.error
    ]
    end_error_users = {int(a.user_id) for a in still_error_rows}
    stats["recovered"] = recovered
    stats["remaining"] = len(still_error_rows)
    stats["users_remaining"] = len(end_error_users)
    stats["users_recovered"] = len(start_error_users - end_error_users)
    logger.info(
        "retry-all attempted=%s recovered=%s remaining_err=%s skipped=%s "
        "ok_panels=%s down_panels=%s budget=%.0fs dt=%.2fs",
        stats["attempted"],
        recovered,
        stats["remaining"],
        skipped,
        ",".join(panels_ok_names) or "-",
        ",".join(panels_down_names) or "-",
        budget,
        time.monotonic() - t0,
    )
    return stats


async def drop_inactive_panel_errors(db: AsyncSession, panel_id: int) -> int:
    """When a panel is turned off, clear its error/pending rows on all users."""
    result = await db.execute(
        select(PanelAccount).where(
            PanelAccount.panel_id == int(panel_id),
            PanelAccount.status.in_(
                (PanelAccountStatus.error, PanelAccountStatus.pending)
            ),
        )
    )
    rows = list(result.scalars().all())
    for a in rows:
        a.status = PanelAccountStatus.deleted
        a.error_message = None
        a.links = []
        a.links_by_group = {}
    if rows:
        await db.flush()
        logger.info(
            "drop inactive-panel errors panel=%s count=%s", panel_id, len(rows)
        )
    return len(rows)


async def heal_failed_panel_accounts_batch(
    *,
    limit_users: int = 25,
) -> int:
    """Retry error/pending panel accounts (probe-first, parallel by panel).

    Used by background heal loop and subscription maintain — when a panel
    comes back online, configs appear on the next successful heal.
    """
    if _bulk_retry_lock.locked():
        return 0
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        try:
            stats = await retry_all_failed_accounts(
                db,
                overall_timeout=120.0,
                per_remote_timeout=20.0,
                probe_timeout=10.0,
                concurrency=16,
                limit_accounts=max(40, int(limit_users) * 4),
            )
            healed = int(stats.get("users_recovered") or 0)
            if healed:
                logger.info(
                    "heal bulk recovered_users=%s remaining=%s",
                    healed,
                    stats.get("remaining"),
                )
            return healed
        except Exception as exc:  # noqa: BLE001
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
            logger.warning("heal bulk retry failed: %s", _exc_msg(exc))
            return 0


def _normalize_config_tag(raw: object) -> str:
    return " ".join(str(raw or "").split()).strip()[:24]


def _link_protocol(link: str) -> str:
    """Normalize share-link scheme to a protocol key (vless / shadowsocks / …)."""
    raw = (link or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("ss://"):
        return "shadowsocks"
    if "://" not in raw:
        return ""
    scheme = raw.split("://", 1)[0].strip()
    if scheme in ("ss", "shadowsocks"):
        return "shadowsocks"
    if scheme in ("hy2", "hysteria2"):
        return "hysteria2"
    return scheme


def _group_config_protocols(config: dict | None) -> set[str]:
    """Protocols this group expects on its panel (from GroupPanel.config)."""
    cfg = config or {}
    out: set[str] = set()
    for p in cfg.get("protocols") or []:
        key = str(p or "").strip().lower()
        if not key:
            continue
        if key in ("ss", "shadowsocks"):
            out.add("shadowsocks")
        elif key in ("hy2", "hysteria2"):
            out.add("hysteria2")
        else:
            out.add(key)
    inbounds = cfg.get("inbounds")
    if isinstance(inbounds, dict):
        for k in inbounds.keys():
            key = str(k or "").strip().lower()
            if not key:
                continue
            if key in ("ss", "shadowsocks"):
                out.add("shadowsocks")
            elif key in ("hy2", "hysteria2"):
                out.add("hysteria2")
            else:
                out.add(key)
    return out


def _link_fit_group_config(link: str, config: dict | None) -> str:
    """How well a share link matches a group's panel config.

    Returns:
      - ``specific``: group filters by protocol and this link matches
      - ``broad``: group has no protocol filter (PasarGuard/Sanaei) — claims all on panel
      - ``no``: group filters by protocol and this link does not match
    """
    protos = _group_config_protocols(config)
    if not protos:
        return "broad"
    lp = _link_protocol(link)
    if not lp or lp in ("http", "https"):
        # Not yet expanded /sub or unknown — don't force a protocol group
        return "broad"
    return "specific" if lp in protos else "no"


def _remote_group_ids(config: dict | None) -> list[int]:
    cfg = config or {}
    raw = cfg.get("remote_group_ids") or cfg.get("group_ids") or []
    out: list[int] = []
    for x in raw:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _partition_links_by_protocol(
    all_links: list[str],
    claims: list[dict],
) -> dict[str, list[str]] | None:
    """Split links by Marzban-style protocol filters.

    Returns None when any claim has no protocol filter (needs remote partition)
    while multiple claims share the panel — caller must use remote_group split.
    """
    from app.adapters.http_utils import is_share_link

    if not claims:
        return {}
    specific = [c for c in claims if _group_config_protocols(c.get("config"))]
    broad = [c for c in claims if not _group_config_protocols(c.get("config"))]
    # Ambiguous PasarGuard/Sanaei multi-group share → cannot protocol-split
    if len(broad) > 1:
        return None
    if not specific and len(claims) == 1:
        gid = str(claims[0]["group_id"])
        return {gid: [l for l in all_links if is_share_link(l)]}

    out: dict[str, list[str]] = {str(c["group_id"]): [] for c in claims}
    assigned: set[str] = set()
    for link in all_links:
        if not is_share_link(link) or link in assigned:
            continue
        hit = False
        for c in specific:
            if _link_fit_group_config(link, c.get("config")) == "specific":
                out[str(c["group_id"])].append(link)
                assigned.add(link)
                hit = True
                break
        if hit:
            continue
        # leftovers → single broad claim, else first specific
        if broad:
            out[str(broad[0]["group_id"])].append(link)
            assigned.add(link)
        elif specific:
            out[str(specific[0]["group_id"])].append(link)
            assigned.add(link)
    return out


_partition_locks: dict[str, asyncio.Lock] = {}


async def _partition_pasarguard_by_remote_groups(
    user: User,
    account: PanelAccount,
    claims: list[dict],
) -> dict[str, list[str]] | None:
    """Fetch link sets per remote_group_ids by briefly switching PG group membership.

    Restores original group_ids afterwards. Locked per remote user.
    """
    from app.adapters.base import UserPayload
    from app.adapters.http_utils import expand_subscription_links, is_share_link, make_client

    panel = account.__dict__.get("panel")
    if panel is None:
        return None
    ptype = getattr(panel, "type", None)
    ptype_s = ptype.value if hasattr(ptype, "value") else str(ptype or "")
    if ptype_s != "pasarguard":
        return None

    # frozenset(remote_gids) → [astra group_ids]
    buckets: dict[frozenset[int], list[int]] = {}
    for c in claims:
        rids = frozenset(_remote_group_ids(c.get("config")))
        if not rids:
            return None
        buckets.setdefault(rids, []).append(int(c["group_id"]))
    if len(buckets) <= 1:
        return None

    uname = (account.remote_username or user.username or "").strip()
    if not uname:
        return None

    lock_key = f"{panel.id}:{uname}"
    lock = _partition_locks.setdefault(lock_key, asyncio.Lock())
    adapter = get_adapter(panel)

    async with lock:
        try:
            cur = await adapter.get_usage(uname, expand_links=False)
            if not cur or not cur.success:
                return None
            raw = cur.raw if isinstance(cur.raw, dict) else {}
            original_gids = [int(x) for x in (raw.get("group_ids") or []) if x is not None]
            if not original_gids:
                original_gids = sorted({i for s in buckets for i in s})

            base_payload = UserPayload(
                username=uname,
                data_limit=int(user.data_limit or 0),
                expire=int(user.expire or 0) or None,
                status=user.status.value if hasattr(user.status, "value") else str(user.status),
                note=getattr(user, "note", None),
                config={},
            )
            out: dict[str, list[str]] = {}
            client = make_client(
                panel.base_url,
                bool((panel.meta or {}).get("verify_ssl", False)),
                timeout=8.0,
                xhr=False,
            )
            try:
                for rids, gids in buckets.items():
                    base_payload.config = {"remote_group_ids": list(rids), "group_ids": list(rids)}
                    mod = await adapter.modify_user(base_payload)
                    if not mod.success:
                        logger.warning(
                            "tag-partition modify failed user=%s gids=%s: %s",
                            uname,
                            list(rids),
                            mod.error,
                        )
                        continue
                    res = await adapter.get_usage(uname, expand_links=False)
                    raw_links = [str(x).strip() for x in (res.links or []) if x]
                    shares = [x for x in raw_links if is_share_link(x)]
                    if not shares and raw_links:
                        try:
                            shares = await asyncio.wait_for(
                                expand_subscription_links(client, raw_links), timeout=10
                            )
                        except Exception:  # noqa: BLE001
                            shares = []
                    shares = [x for x in (shares or []) if is_share_link(x)]
                    for gid in gids:
                        out[str(gid)] = list(shares)
            finally:
                await client.aclose()
                # Always restore full membership
                base_payload.config = {
                    "remote_group_ids": original_gids,
                    "group_ids": original_gids,
                }
                try:
                    await adapter.modify_user(base_payload)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("tag-partition restore failed user=%s: %s", uname, exc)

            return out if out else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("tag-partition error user=%s: %s", uname, exc)
            return None


async def ensure_account_links_by_group(
    user: User,
    account: PanelAccount,
    claims: list[dict],
    *,
    allow_remote: bool = False,
) -> dict[str, list[str]]:
    """Return astra_group_id → links for this panel account (partitioned).

    On the subscription GET path pass ``allow_remote=False`` so we never hit
    remotes (stable + fast). BG/provision may pass ``allow_remote=True``.
    """
    from app.adapters.http_utils import is_share_link

    panel_claims = [c for c in claims if int(c.get("panel_id") or 0) == int(account.panel_id)]
    all_links = [str(x).strip() for x in (account.links or []) if x and is_share_link(str(x))]
    if not panel_claims:
        return {}
    if not all_links:
        return {str(c["group_id"]): [] for c in panel_claims}

    if len(panel_claims) == 1:
        gid = str(panel_claims[0]["group_id"])
        mapping = {gid: list(all_links)}
        account.links_by_group = mapping
        return mapping

    # 1) Protocol split (Marzban Direct=vless / Tunnel=ss)
    local = _partition_links_by_protocol(all_links, panel_claims)
    if local is not None:
        account.links_by_group = local
        return local

    # 2) Cached remote partition — only when it covers EVERY share link
    cached = getattr(account, "links_by_group", None) or {}
    if isinstance(cached, dict) and cached:
        want = {str(c["group_id"]) for c in panel_claims}
        if want <= {str(k) for k in cached.keys()}:
            total = sum(
                len(v or []) for k, v in cached.items() if str(k) in want
            )
            if total == len(all_links):
                return {
                    str(k): list(v or [])
                    for k, v in cached.items()
                    if str(k) in want
                }

    # 3) PasarGuard remote partition — never on hot sub GET
    if allow_remote:
        remote = await _partition_pasarguard_by_remote_groups(
            user, account, panel_claims
        )
        if remote:
            account.links_by_group = remote
            return remote

    # 4) Last resort — first claim only (avoids wrong tag on every group)
    gid = str(panel_claims[0]["group_id"])
    mapping = {gid: list(all_links)}
    for c in panel_claims[1:]:
        mapping.setdefault(str(c["group_id"]), [])
    account.links_by_group = mapping
    return mapping


async def resolve_group_panel_claims(db: AsyncSession, user: User) -> list[dict]:
    """User's group→panel claims with config_tag + GroupPanel.config (fresh from DB).

    Excludes inactive panels and disabled sales groups so every sub respects
    admin filters consistently.
    """
    from sqlalchemy import select

    from app.models.group import Group, GroupPanel, UserGroup
    from app.models.panel import Panel

    ug = await db.execute(select(UserGroup.group_id).where(UserGroup.user_id == user.id))
    gids = [int(r[0]) for r in ug.all() if r[0] is not None]
    if not gids:
        return []

    g_rows = await db.execute(
        select(Group.id, Group.config_tag, Group.name)
        .where(Group.id.in_(gids), Group.is_disabled.is_(False))
        .order_by(Group.id.asc())
    )
    meta_by_gid = {
        int(gid): (_normalize_config_tag(tag), str(name or ""))
        for gid, tag, name in g_rows.all()
    }
    active_gids = list(meta_by_gid.keys())
    if not active_gids:
        return []

    pl = await db.execute(
        select(GroupPanel.group_id, GroupPanel.panel_id, GroupPanel.config)
        .join(Panel, Panel.id == GroupPanel.panel_id)
        .where(
            GroupPanel.group_id.in_(active_gids),
            Panel.is_active.is_(True),
        )
        .order_by(GroupPanel.group_id.asc())
    )
    claims: list[dict] = []
    for gid, pid, cfg in pl.all():
        pid_i = int(pid or 0)
        if not pid_i:
            continue
        tag, name = meta_by_gid.get(int(gid), ("", ""))
        claims.append(
            {
                "group_id": int(gid),
                "panel_id": pid_i,
                "tag": tag,
                "name": name,
                "config": cfg if isinstance(cfg, dict) else {},
            }
        )
    return claims


def _claims_from_loaded_groups(user: User) -> list[dict]:
    groups = list(getattr(user, "groups", None) or user.__dict__.get("groups") or [])
    claims: list[dict] = []
    for group in sorted(groups, key=lambda g: int(getattr(g, "id", 0) or 0)):
        if bool(getattr(group, "is_disabled", False)):
            continue
        tag = _normalize_config_tag(getattr(group, "config_tag", None))
        for gpl in getattr(group, "panel_links", None) or []:
            pid = int(getattr(gpl, "panel_id", 0) or 0)
            if not pid:
                continue
            panel = getattr(gpl, "panel", None)
            if panel is not None and not bool(getattr(panel, "is_active", True)):
                continue
            cfg = getattr(gpl, "config", None)
            claims.append(
                {
                    "group_id": int(getattr(group, "id", 0) or 0),
                    "panel_id": pid,
                    "tag": tag,
                    "name": str(getattr(group, "name", "") or ""),
                    "config": cfg if isinstance(cfg, dict) else {},
                }
            )
    return claims


def _panel_is_active(panel) -> bool:
    if panel is None:
        return False
    return bool(getattr(panel, "is_active", True))


async def resolve_panel_config_tags(db: AsyncSession, user: User) -> dict[int, str]:
    """Legacy panel_id → tag map (first non-empty). Prefer per-link claims instead."""
    claims = await resolve_group_panel_claims(db, user)
    out: dict[int, str] = {}
    for claim in claims:
        pid = int(claim["panel_id"])
        tag = _normalize_config_tag(claim.get("tag"))
        if pid and tag and pid not in out:
            out[pid] = tag
    return out


def _config_hidden(link: str) -> bool:
    """Skip share links whose host:port[+header] is globally hidden from all subs."""
    try:
        from app.services.config_hides import is_hidden

        return bool(is_hidden(link))
    except Exception:  # noqa: BLE001
        return False


async def collect_links(
    user: User,
    *,
    include_notices: bool = False,
    include_hidden: bool = False,
    db: AsyncSession | None = None,
    live_rebuild: bool = True,
) -> list[str]:
    rows = await collect_links_detailed(
        user,
        include_notices=include_notices,
        include_hidden=include_hidden,
        db=db,
        live_rebuild=live_rebuild,
    )
    return [row["link"] for row in rows if row.get("link")]


async def collect_links_detailed(
    user: User,
    *,
    include_notices: bool = True,
    include_hidden: bool = False,
    db: AsyncSession | None = None,
    live_rebuild: bool = True,
) -> list[dict]:
    """All unique share links across panels.

    Order (every sub — client feed + HTML):
      1) status row (✨ user - remain - days ✨) — always, live values
      2) promo rows — up to 2 when enabled in settings
      3) group notices (کانفیگ اضافه) — optional, unchanged text
      4) real configs — ``#N. 2.5G | name`` or ``#N. text 2.5G | name``

    Each real config gets the ``config_tag`` of the Astra group that owns it:
    protocol split for Marzban, remote_group_ids partition for PasarGuard.

    ``include_hidden=True`` keeps globally-hidden endpoints (config-order admin).
    ``live_rebuild=False`` (subscription hot path): use cached account.links +
    in-memory group graph — no Astra live rewrite / no extra claims SQL.
    """
    from app.adapters.http_utils import is_share_link
    from app.services.link_remark import (
        apply_display_name,
        build_promo_row,
        build_status_row,
        notice_rows_from_groups,
    )
    from app.services.site_settings import (
        get_promo_config_texts,
        get_status_line_template,
        get_status_panel_label,
    )

    used = int(user.used_traffic or 0)
    limit = int(user.data_limit or 0)
    # Status line uses login username (stable), not display_name
    status_name = (getattr(user, "username", None) or "user").strip() or "user"
    expire_ts = getattr(user, "expire", None)
    if getattr(user, "status", None) == UserStatus.on_hold:
        hold_dur = _on_hold_duration_seconds(user)
        if hold_dur > 0:
            expire_ts = int(time.time()) + hold_dur
    groups = list(getattr(user, "groups", None) or user.__dict__.get("groups") or [])

    # Prefer already-loaded group→panel graph (no extra SQL) on the hot sub path.
    loaded_claims = _claims_from_loaded_groups(user)
    if loaded_claims or not live_rebuild or db is None:
        claims = loaded_claims
    else:
        claims = await resolve_group_panel_claims(db, user)

    tag_by_gid = {str(c["group_id"]): _normalize_config_tag(c.get("tag")) for c in claims}

    head: list[dict] = [
        build_status_row(
            username=status_name,
            used=used,
            limit=limit,
            expire=expire_ts,
            template=get_status_line_template(),
            panel_label=get_status_panel_label(),
        )
    ]
    for promo_text in get_promo_config_texts():
        promo = build_promo_row(promo_text)
        if promo:
            head.append(promo)

    notices = notice_rows_from_groups(groups) if include_notices else []
    rows: list[dict] = []
    seen: set[str] = set()

    # Only emit configs for live (panel × group) claims — not merely group membership
    # (a panel removed from a group must leave the sub even before remote delete finishes).
    claim_keys = {
        (int(c["panel_id"]), int(c["group_id"]))
        for c in claims
        if c.get("panel_id") is not None and c.get("group_id") is not None
    }
    claim_panel_ids = {pid for pid, _gid in claim_keys}

    def _account_in_membership(account: PanelAccount) -> bool:
        gid = getattr(account, "group_id", None)
        if gid is not None:
            return (int(account.panel_id), int(gid)) in claim_keys
        # Legacy merged row: keep only if panel still claimed by a current group
        return int(account.panel_id) in claim_panel_ids

    active_accounts = [
        a
        for a in (user.panel_accounts or [])
        if a.status in (PanelAccountStatus.active, PanelAccountStatus.error)
        and _panel_is_active(a.__dict__.get("panel") or getattr(a, "panel", None))
        and _account_in_membership(a)
    ]

    # Astra live rewrite is for domain/port changes — skip on subscription GET.
    astra_mutated = False
    if live_rebuild and db is not None:
        from app.models.panel import PanelType
        from app.services import astra_node as astra_node_svc

        astra_links_cache: dict[int, dict[str, list[str]]] = {}
        for acc in active_accounts:
            panel = acc.__dict__.get("panel") or getattr(acc, "panel", None)
            if panel is None or getattr(panel, "type", None) != PanelType.astra:
                continue
            pid = int(acc.panel_id)
            if pid not in astra_links_cache:
                try:
                    astra_links_cache[pid] = await astra_node_svc.share_links_by_email(
                        db, panel
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "astra live links failed panel=%s", pid, exc_info=True
                    )
                    astra_links_cache[pid] = {}
            email = (acc.remote_username or "").strip()
            fresh = list(astra_links_cache[pid].get(email) or [])
            if list(acc.links or []) != fresh:
                acc.links = fresh
                acc.links_by_group = None
                astra_mutated = True

    # Per-group accounts already own one group's links; legacy merged rows still partition
    need_partition = [
        a for a in active_accounts if not getattr(a, "group_id", None)
    ]
    partitioned_map: dict[int, dict[str, list[str]]] = {}
    if need_partition:
        partitioned = await asyncio.gather(
            *[
                ensure_account_links_by_group(
                    user, a, claims, allow_remote=False
                )
                for a in need_partition
            ],
            return_exceptions=True,
        )
        for account, by_group in zip(need_partition, partitioned):
            if isinstance(by_group, Exception) or not isinstance(by_group, dict):
                logger.warning(
                    "links_by_group failed panel=%s: %s", account.panel_id, by_group
                )
                partitioned_map[account.id] = {}
            else:
                partitioned_map[account.id] = by_group

    for account in active_accounts:
        panel = account.__dict__.get("panel")
        panel_name = getattr(panel, "name", None) if panel is not None else None
        label = panel_name or f"پنل #{account.panel_id}"
        pid = int(account.panel_id)
        acct_gid = getattr(account, "group_id", None)

        if acct_gid is not None:
            # Dedicated per-group remote account — all its links belong to that group
            gid_s = str(int(acct_gid))
            tag = tag_by_gid.get(gid_s, "")
            for link in account.links or []:
                text = (link or "").strip()
                if not text or text in seen or not is_share_link(text):
                    continue
                if not include_hidden and _config_hidden(text):
                    continue
                seen.add(text)
                rows.append(
                    {
                        "link": text,
                        "panel": label,
                        "panel_id": pid,
                        "config_tag": tag,
                        "group_id": int(acct_gid),
                    }
                )
            continue

        panel_claims = [c for c in claims if int(c.get("panel_id") or 0) == pid]
        by_group = partitioned_map.get(account.id) or {}

        if not panel_claims:
            for link in account.links or []:
                text = (link or "").strip()
                if not text or text in seen or not is_share_link(text):
                    continue
                if not include_hidden and _config_hidden(text):
                    continue
                seen.add(text)
                rows.append(
                    {"link": text, "panel": label, "panel_id": pid, "config_tag": ""}
                )
            continue

        if not by_group:
            local = _partition_links_by_protocol(
                [str(x).strip() for x in (account.links or []) if x],
                panel_claims,
            )
            if local:
                by_group = local
            else:
                by_group = {
                    str(panel_claims[0]["group_id"]): [
                        str(x).strip()
                        for x in (account.links or [])
                        if x and is_share_link(str(x))
                    ]
                }

        for gid in sorted(by_group.keys(), key=lambda x: int(x) if str(x).isdigit() else 0):
            tag = tag_by_gid.get(str(gid), "")
            gid_i = int(gid) if str(gid).isdigit() else 0
            for link in by_group.get(gid) or []:
                text = (link or "").strip()
                if not text or text in seen or not is_share_link(text):
                    continue
                if not include_hidden and _config_hidden(text):
                    continue
                seen.add(text)
                rows.append(
                    {
                        "link": text,
                        "panel": label,
                        "panel_id": pid,
                        "config_tag": tag,
                        "group_id": gid_i,
                    }
                )

    if astra_mutated and db is not None:
        try:
            await db.flush()
        except Exception:  # noqa: BLE001
            pass

    # Global admin order by host:port[+SNI]; unlisted configs stay at the end.
    # Users missing a panel simply won't have that link — nothing is injected.
    from app.services.config_locations import (
        extract_link_endpoint,
        extract_link_match_host,
        try_normalize_endpoint,
    )
    from app.services.config_orders import link_order_index

    rows.sort(
        key=lambda row: (
            link_order_index(str(row.get("link") or "")),
            int(row.get("panel_id") or 0),
            str(row.get("link") or ""),
        )
    )

    # One share-link per endpoint fingerprint so sub #N matches config-order #N.
    # Same host:port[+SNI] with different UUIDs collapse (multi-panel duplicates).
    # Distinct CDN paths (Astra WS/XHTTP on one domain:port) must BOTH stay.
    from app.services.config_locations import extract_link_path

    seen_fp: set[tuple] = set()
    deduped: list[dict] = []
    for row in rows:
        link = str(row.get("link") or "")
        ep = extract_link_endpoint(link)
        if ep:
            mh = extract_link_match_host(link) or ""
            norm = try_normalize_endpoint(ep[0], ep[1], mh)
            if norm:
                key: tuple = (*norm, extract_link_path(link))
                if key in seen_fp:
                    continue
                seen_fp.add(key)
        deduped.append(row)
    rows = deduped

    renamed = apply_display_name(
        rows,
        getattr(user, "display_name", None),
        used=used,
        limit=limit,
        username=getattr(user, "username", None),
    )
    return head + notices + renamed


async def hydrate_share_links(db: AsyncSession, user: User) -> int:
    """Fetch share links for incomplete accounts — HTTP in parallel, ORM apply serial.

    Concurrent mutation of the same AsyncSession caused StaleDataError /
    PendingRollbackError (500 on /sub). Network I/O stays parallel; writing
    ``PanelAccount`` fields happens only on the request thread afterward.
    """
    from app.adapters.http_utils import (
        expand_subscription_links,
        is_share_link,
        is_subscription_ref,
        make_client,
    )

    def _needs_hydrate(account: PanelAccount) -> bool:
        if account.status not in (
            PanelAccountStatus.active,
            PanelAccountStatus.error,
            PanelAccountStatus.pending,
        ):
            return False
        if not account.panel or not _panel_is_active(account.panel):
            return False
        links = [str(x).strip() for x in (account.links or []) if x and str(x).strip()]
        if any(is_subscription_ref(x) for x in links):
            return True
        if any(is_share_link(x) for x in links):
            if account.status == PanelAccountStatus.error:
                account.status = PanelAccountStatus.active
            return False
        return True

    targets = [a for a in (user.panel_accounts or []) if _needs_hydrate(a)]
    if not targets:
        return 0

    lock = await _lock_for_user(int(user.id))
    async with lock:
        # Re-check after lock — another request may have finished hydrate.
        targets = [a for a in (user.panel_accounts or []) if _needs_hydrate(a)]
        if not targets:
            return 0

        now = datetime.now(timezone.utc)
        sem = asyncio.Semaphore(max(8, min(32, len(targets) + 4)))

        async def _expand_refs(panel, refs: list[str]) -> list[str]:
            client = make_client(
                panel.base_url,
                bool((panel.meta or {}).get("verify_ssl", False)),
                timeout=16.0,
                xhr=False,
            )
            try:
                expanded = await expand_subscription_links(client, refs)
            except Exception:
                expanded = []
            finally:
                await client.aclose()
            return [x for x in (expanded or []) if is_share_link(x)]

        async def _fetch_one(account: PanelAccount) -> dict | None:
            """Pure network fetch — never touch ORM attributes."""
            async with sem:
                panel = account.panel
                aid = int(account.id)
                stored = [
                    str(x).strip()
                    for x in (account.links or [])
                    if x and str(x).strip()
                ]
                before_shares = [x for x in stored if is_share_link(x)]
                before_n = len(before_shares)
                uname = (account.remote_username or user.username or "").strip()
                rid = account.remote_id or account.remote_username or uname or None
                adapter = None
                try:
                    if any(is_subscription_ref(x) for x in stored):
                        try:
                            shares = await asyncio.wait_for(
                                _expand_refs(panel, stored), timeout=15.0
                            )
                        except Exception:
                            shares = []
                        if shares and len(shares) >= max(1, before_n):
                            return {
                                "account_id": aid,
                                "links": shares,
                                "shares": True,
                            }

                    adapter = get_adapter(panel)
                    try:
                        coro = adapter.get_usage(uname, rid, expand_links=False)  # type: ignore[call-arg]
                    except TypeError:
                        coro = adapter.get_usage(uname, rid)
                    res = await asyncio.wait_for(coro, timeout=8.0)
                    if not res or not res.success:
                        return None

                    raw_links = [
                        str(x).strip()
                        for x in (res.links or [])
                        if x and str(x).strip()
                    ]
                    shares = [x for x in raw_links if is_share_link(x)]
                    if any(is_subscription_ref(x) for x in raw_links):
                        try:
                            expanded = await asyncio.wait_for(
                                _expand_refs(panel, raw_links), timeout=15.0
                            )
                        except Exception:
                            expanded = []
                        if expanded and len(expanded) >= len(shares):
                            shares = expanded

                    out_links: list[str] | None = None
                    if shares and len(shares) >= max(1, before_n):
                        out_links = shares
                    elif raw_links and not before_shares:
                        out_links = raw_links
                    elif before_shares:
                        return None
                    else:
                        return None

                    return {
                        "account_id": aid,
                        "links": out_links,
                        "shares": bool(shares),
                        "remote_username": res.remote_username or uname,
                        "remote_id": res.remote_id,
                        "uuid": res.uuid,
                        "used_traffic": int(
                            res.used_traffic or account.used_traffic or 0
                        ),
                    }
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "hydrate links failed user=%s panel=%s: %s",
                        user.username,
                        account.panel_id,
                        _exc_msg(exc),
                    )
                    return None
                finally:
                    if adapter is not None:
                        close = getattr(adapter, "close", None)
                        if callable(close):
                            try:
                                await close()
                            except Exception:  # noqa: BLE001
                                pass

        async def _fetch_bounded(account: PanelAccount) -> dict | None:
            try:
                return await asyncio.wait_for(_fetch_one(account), timeout=16.0)
            except Exception:
                return None

        raw_results = await asyncio.gather(
            *[_fetch_bounded(a) for a in targets],
            return_exceptions=True,
        )

        by_id = {int(a.id): a for a in targets}
        updated = 0
        for item in raw_results:
            if not isinstance(item, dict) or not item.get("links"):
                continue
            account = by_id.get(int(item["account_id"]))
            if account is None:
                continue
            account.links = list(item["links"])
            account.links_by_group = None
            account.status = PanelAccountStatus.active
            account.error_message = None
            account.last_sync_at = now
            if item.get("remote_username"):
                account.remote_username = item["remote_username"]
            if item.get("remote_id"):
                account.remote_id = item["remote_id"]
            if item.get("uuid"):
                account.uuid = item["uuid"]
            if item.get("used_traffic") is not None:
                account.used_traffic = int(item["used_traffic"])
            panel = account.panel
            if panel is not None:
                panel.last_ok_at = now
            updated += 1

        if updated:
            try:
                await db.flush()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "hydrate flush failed user=%s: %s", user.username, _exc_msg(exc)
                )
                try:
                    await db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                # Re-load is caller's job; report 0 so we don't commit a dead session
                return 0

        logger.info(
            "hydrate user=%s targets=%s updated=%s remaining=%s",
            user.username,
            len(targets),
            updated,
            len([a for a in (user.panel_accounts or []) if _needs_hydrate(a)]),
        )
        return updated


async def usage_breakdown_for_user(db: AsyncSession, user: User) -> list[dict]:
    """Per-panel raw usage with live coefficient → weighted (admin / internal)."""
    accounts = list(user.panel_accounts or [])
    account_coeffs = await account_coefficients_for_user(db, user, accounts)
    _sync_account_coeff_periods(accounts, account_coeffs)
    user_limit = int(user.data_limit or 0)
    weighted_total = await _weighted_total(accounts, account_coeffs)
    rows: list[dict] = []
    for account in accounts:
        if account.status not in (
            PanelAccountStatus.active,
            PanelAccountStatus.error,
        ):
            continue
        panel = account.__dict__.get("panel")
        name = getattr(panel, "name", None) if panel is not None else None
        label = name or f"پنل #{account.panel_id}"
        raw = int(account.used_traffic or 0)
        coeff = _norm_coeff(account_coeffs.get(account.id, 1.0))
        weighted = account_weighted_usage(account, coeff)
        remote_limit = remote_data_limit_bytes(
            user_limit,
            {"coefficient": coeff},
            used_weighted_total=weighted_total,
            used_raw_on_panel=raw,
        )
        n_links = len([x for x in (account.links or []) if x])
        rows.append(
            {
                "panel": label,
                "panel_id": account.panel_id,
                "group_id": int(getattr(account, "group_id", 0) or 0) or None,
                "raw": raw,
                "coefficient": coeff,
                "weighted": weighted,
                "remote_limit": remote_limit,
                "configs": n_links,
            }
        )
    rows.sort(key=lambda r: r["weighted"], reverse=True)
    return rows


async def panel_coefficients_for_user(db: AsyncSession, user: User) -> dict[int, float]:
    """Legacy map panel_id → max coefficient (UI fallback only).

    Prefer ``account_coefficients_for_user`` — max was the shared-panel bug.
    """
    ug = await db.execute(select(UserGroup.group_id).where(UserGroup.user_id == user.id))
    group_ids = [row[0] for row in ug.all()]
    if not group_ids:
        return {}
    result = await db.execute(select(GroupPanel).where(GroupPanel.group_id.in_(group_ids)))
    coeffs: dict[int, float] = {}
    for link in result.scalars().all():
        c = coefficient_from_config(link.config if isinstance(link.config, dict) else {})
        prev = coeffs.get(link.panel_id)
        coeffs[link.panel_id] = max(prev, c) if prev is not None else c
    return coeffs


async def claim_coefficients_for_user(
    db: AsyncSession, user: User
) -> dict[tuple[int, int], float]:
    """Map (panel_id, group_id) → coefficient from GroupPanel.config."""
    ug = await db.execute(select(UserGroup.group_id).where(UserGroup.user_id == user.id))
    group_ids = [row[0] for row in ug.all()]
    if not group_ids:
        return {}
    result = await db.execute(select(GroupPanel).where(GroupPanel.group_id.in_(group_ids)))
    out: dict[tuple[int, int], float] = {}
    for link in result.scalars().all():
        out[(int(link.panel_id), int(link.group_id))] = coefficient_from_config(
            link.config if isinstance(link.config, dict) else {}
        )
    return out


async def account_coefficients_for_user(
    db: AsyncSession,
    user: User,
    accounts: list[PanelAccount] | None = None,
) -> dict[int, float]:
    """Map PanelAccount.id → that account's group×panel coefficient.

    Formula: used_astra = Σ account.raw × account_coefficients[account.id]
    """
    claims = await claim_coefficients_for_user(db, user)
    panel_max = await panel_coefficients_for_user(db, user)
    if accounts is None:
        result = await db.execute(
            select(PanelAccount).where(PanelAccount.user_id == user.id)
        )
        accounts = list(result.scalars().all())
    out: dict[int, float] = {}
    for account in accounts:
        gid = getattr(account, "group_id", None)
        if gid is not None:
            out[account.id] = _norm_coeff(
                claims.get((int(account.panel_id), int(gid)), 1.0)
            )
        else:
            # Legacy merged row: fall back to panel max until split on next provision
            out[account.id] = _norm_coeff(panel_max.get(int(account.panel_id), 1.0))
    return out


async def device_limit_for_user(db: AsyncSession, user: User) -> int:
    """Max simultaneous devices/IPs from group panel configs (0 = unlimited / unset)."""
    ug = await db.execute(select(UserGroup.group_id).where(UserGroup.user_id == user.id))
    group_ids = [row[0] for row in ug.all()]
    if not group_ids:
        return 0
    result = await db.execute(select(GroupPanel).where(GroupPanel.group_id.in_(group_ids)))
    best = 0
    for link in result.scalars().all():
        cfg = link.config or {}
        raw = cfg.get("limit_ip", cfg.get("ip_limit", 0))
        try:
            n = int(raw or 0)
        except (TypeError, ValueError):
            n = 0
        if n > best:
            best = n
    return best


def _sync_account_coeff_periods(
    accounts: list[PanelAccount], account_coeffs: dict[int, float]
) -> bool:
    """Apply current group coefficients to account periods. True if any changed."""
    changed = False
    for account in accounts:
        if account.status not in (
            PanelAccountStatus.active,
            PanelAccountStatus.error,
            PanelAccountStatus.pending,
        ):
            continue
        coeff = _norm_coeff(account_coeffs.get(account.id, 1.0))
        if apply_coefficient_period(account, coeff):
            changed = True
    return changed


async def _weighted_total(
    accounts: list[PanelAccount], account_coeffs: dict[int, float]
) -> int:
    """Σ (raw_i × coeff_of_account_i's group)."""
    _sync_account_coeff_periods(accounts, account_coeffs)
    total = 0
    for account in accounts:
        if account.status not in (
            PanelAccountStatus.active,
            PanelAccountStatus.error,
        ):
            continue
        coeff = _norm_coeff(account_coeffs.get(account.id, 1.0))
        total += account_weighted_usage(account, coeff)
    return total


def _as_utc(dt: datetime) -> datetime:
    """Normalize DB datetimes (SQLite often returns naive) to aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _accounts_need_remote_refresh(accounts: list[PanelAccount], interval: int) -> bool:
    """True if any active account is stale / never synced."""
    if interval <= 0:
        return True
    now = datetime.now(timezone.utc)
    actives = [a for a in accounts if a.status == PanelAccountStatus.active and a.panel]
    if not actives:
        return False
    for a in actives:
        if not a.last_sync_at:
            return True
        age = (now - _as_utc(a.last_sync_at)).total_seconds()
        if age >= interval:
            return True
    return False


def _active_accounts(accounts: list[PanelAccount]) -> list[PanelAccount]:
    return [
        a
        for a in accounts
        if a.status in (PanelAccountStatus.active, PanelAccountStatus.error)
    ]


async def should_push_remotes(
    db: AsyncSession,
    user: User,
    accounts: list[PanelAccount] | None = None,
    *,
    status_changed: bool = False,
    force: bool = False,
    shrink_shared_caps: bool = False,
) -> bool:
    blocked = user.status in (
        UserStatus.limited,
        UserStatus.expired,
        UserStatus.disabled,
    )
    if accounts is None:
        result = await db.execute(
            select(PanelAccount).where(PanelAccount.user_id == user.id)
        )
        accounts = list(result.scalars().all())
    active = _active_accounts(accounts)
    account_coeffs = await account_coefficients_for_user(db, user, accounts)
    return quota_math.needs_remote_cap_sync(
        status_changed=status_changed,
        blocked=blocked,
        force=force,
        active_panel_count=len(active),
        coefficients=[account_coeffs.get(a.id, 1.0) for a in active],
        shrink_shared_caps=shrink_shared_caps,
    )


async def refresh_users_usage_batch(
    db: AsyncSession,
    users: list[User],
    *,
    force: bool = False,
    concurrency: int = 10,
    push_remotes: bool = False,
    quick: bool = False,
    isolated: bool = False,
) -> None:
    """Live-pull usage for many users. Push remotes when shared caps / disable needed.

    quick=True → short timeouts / single try (admin list background sync).
    isolated=True → each user uses its own short-lived session + commit (SQLite-safe).
    """
    if not users:
        return
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _work(session: AsyncSession, u: User) -> None:
        await refresh_usage(session, u, force=force, quick=quick)
        # used_traffic is already Σ(raw × coeff) from refresh_usage
        activated = await activate_on_hold_if_used(session, u)
        changed = bool(activated) or await enforce_user_quota(session, u)
        blocked = u.status in (
            UserStatus.limited,
            UserStatus.expired,
            UserStatus.disabled,
        )
        # Quick path: still push when quota just flipped OR user is blocked
        # so every panel stays frozen after volume (with coeff) is exhausted.
        if quick and not changed and not blocked:
            return
        result = await session.execute(
            select(PanelAccount).where(PanelAccount.user_id == u.id)
        )
        accounts = list(result.scalars().all())
        multi = len(_active_accounts(accounts)) > 1
        if await should_push_remotes(
            session,
            u,
            accounts,
            status_changed=changed,
            force=bool(push_remotes) or blocked,
            shrink_shared_caps=multi,
        ):
            await sync_modify_user(session, u)

    async def _one(u: User) -> None:
        async with sem:
            if not isolated:
                try:
                    await _work(db, u)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "batch usage refresh failed user=%s: %s",
                        getattr(u, "username", "?"),
                        exc,
                    )
                return

            from app.core.database import AsyncSessionLocal

            uid = int(u.id)
            uname = getattr(u, "username", "?")
            # Two short transactions: (1) pull usage + enforce quota
            # (2) optional remote push. Never hold a SQLite write lock across
            # panel HTTP inside sync_modify together with usage scrape.
            try:
                async with AsyncSessionLocal() as session:
                    result = await session.execute(
                        select(User)
                        .where(User.id == uid)
                        .options(
                            selectinload(User.panel_accounts).selectinload(
                                PanelAccount.panel
                            )
                        )
                    )
                    fresh = result.scalar_one_or_none()
                    if fresh is None:
                        return
                    await refresh_usage(session, fresh, force=force, quick=quick)
                    activated = await activate_on_hold_if_used(session, fresh)
                    changed = bool(activated) or await enforce_user_quota(
                        session, fresh
                    )
                    blocked = fresh.status in (
                        UserStatus.limited,
                        UserStatus.expired,
                        UserStatus.disabled,
                    )
                    await session.commit()
                    need_push = not (quick and not changed and not blocked)
                if not need_push:
                    return
                async with AsyncSessionLocal() as session:
                    result = await session.execute(
                        select(User)
                        .where(User.id == uid)
                        .options(
                            selectinload(User.panel_accounts).selectinload(
                                PanelAccount.panel
                            )
                        )
                    )
                    fresh = result.scalar_one_or_none()
                    if fresh is None:
                        return
                    result = await session.execute(
                        select(PanelAccount).where(PanelAccount.user_id == uid)
                    )
                    accounts = list(result.scalars().all())
                    multi = len(_active_accounts(accounts)) > 1
                    blocked = fresh.status in (
                        UserStatus.limited,
                        UserStatus.expired,
                        UserStatus.disabled,
                    )
                    if await should_push_remotes(
                        session,
                        fresh,
                        accounts,
                        status_changed=changed,
                        force=bool(push_remotes) or blocked,
                        shrink_shared_caps=multi,
                    ):
                        await sync_modify_user(session, fresh)
                    await session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "batch usage refresh failed user=%s: %s", uname, exc
                )

    await asyncio.gather(*[_one(u) for u in users])


async def refresh_usage(
    db: AsyncSession,
    user: User,
    *,
    force: bool = False,
    min_interval: int | None = None,
    quick: bool = False,
) -> int:
    """Pull live usage from remote panels and apply Astra coefficients.

    PanelAccount.used_traffic = raw bytes from panel.
    User.used_traffic = Σ (weighted_i) across ALL panels for this user.
    """
    settings = get_settings()
    interval = settings.USAGE_REFRESH_SECONDS if min_interval is None else min_interval

    result = await db.execute(
        select(PanelAccount)
        .where(PanelAccount.user_id == user.id)
        .options(selectinload(PanelAccount.panel))
    )
    accounts = list(result.scalars().all())

    # Split legacy merged panel accounts into per-group remotes when needed
    # so coefficients stay exact on shared panels.
    # NEVER on quick/sub path — provision of 20+ panels blocks the event loop
    # and causes nginx 502 on subscription feeds.
    if not quick:
        try:
            ug = await db.execute(
                select(UserGroup.group_id).where(UserGroup.user_id == user.id)
            )
            gids = [row[0] for row in ug.all()]
            targets = await resolve_panels_for_groups(db, gids) if gids else []
            have = {
                (int(a.panel_id), int(a.group_id))
                for a in accounts
                if getattr(a, "group_id", None) is not None
            }
            legacy_panels = {
                int(a.panel_id)
                for a in accounts
                if getattr(a, "group_id", None) is None
                and a.status
                in (
                    PanelAccountStatus.active,
                    PanelAccountStatus.error,
                    PanelAccountStatus.pending,
                )
            }
            missing = False
            for panel, _cfg, gid in targets:
                key = (int(panel.id), int(gid))
                if key in have:
                    continue
                # Legacy row can cover one group on first provision; still need split
                # when this panel has 2+ group targets.
                n_on_panel = sum(1 for p, _, _g in targets if p.id == panel.id)
                if n_on_panel > 1 or int(panel.id) not in legacy_panels:
                    missing = True
                    break
            if missing and targets:
                await provision_user_on_panels(db, user, targets)
                result = await db.execute(
                    select(PanelAccount)
                    .where(PanelAccount.user_id == user.id)
                    .options(selectinload(PanelAccount.panel))
                )
                accounts = list(result.scalars().all())
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "per-group split user=%s: %s",
                getattr(user, "username", "?"),
                _exc_msg(exc),
            )
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
            # Reload accounts after rollback so the rest of refresh can continue
            try:
                result = await db.execute(
                    select(PanelAccount)
                    .where(PanelAccount.user_id == user.id)
                    .options(selectinload(PanelAccount.panel))
                )
                accounts = list(result.scalars().all())
            except Exception:  # noqa: BLE001
                pass

    account_coeffs = await account_coefficients_for_user(db, user, accounts)

    # Same-session Astra traffic sync — avoids nested SQLite sessions in the
    # Astra adapter that used to stall / miss usage until the admin UI opened.
    astra_synced: set[int] = set()
    try:
        from app.services import astra_node as astra_node_svc

        for account in accounts:
            panel = account.panel
            if (
                not panel
                or panel.type != PanelType.astra
                or not _panel_is_active(panel)
            ):
                continue
            pid = int(panel.id)
            if pid in astra_synced:
                continue
            try:
                await astra_node_svc.sync_traffic_from_xray(db, panel)
                astra_synced.add(pid)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "inline astra sync failed panel=%s", pid, exc_info=True
                )
        # Mirror Astra client cumulatives onto this user's accounts
        for account in accounts:
            panel = account.panel
            if not panel or panel.type != PanelType.astra:
                continue
            if account.status not in (
                PanelAccountStatus.active,
                PanelAccountStatus.error,
            ):
                continue
            email = (account.remote_username or user.username or "").strip()
            if not email:
                continue
            client = await astra_node_svc.get_client(db, panel.id, email)
            if not client:
                continue
            got = int(client.used_up or 0) + int(client.used_down or 0)
            account.used_traffic = got
            account.last_sync_at = datetime.now(timezone.utc)
            if user.status == UserStatus.on_hold and got > 0:
                setattr(user, "_hold_remote_activated", True)
    except Exception:  # noqa: BLE001
        logger.debug("astra inline usage apply failed", exc_info=True)

    waiting_hold = (
        user.status == UserStatus.on_hold and _on_hold_duration_seconds(user) > 0
    )
    need_remote = (
        force
        or waiting_hold
        or _accounts_need_remote_refresh(accounts, interval)
    )
    if need_remote:
        # quick: fail-fast (sub bg / admin list); full: longer for forced sync
        if quick:
            per_try = 2.8
            tries = 1
            overall_budget = 5.0
        elif force:
            per_try = 12.0
            tries = 2
            overall_budget = 22.0
        else:
            per_try = 5.0
            tries = 1
            overall_budget = 10.0

        async def _remote(account: PanelAccount) -> tuple[PanelAccount, AdapterResult | None]:
            # Pull usage for active (and error — may still exist on remote)
            if (
                not account.panel
                or not _panel_is_active(account.panel)
                or account.status
                not in (
                    PanelAccountStatus.active,
                    PanelAccountStatus.error,
                )
            ):
                return account, None
            # Astra already applied above — skip nested-session HTTP
            if account.panel.type == PanelType.astra:
                return account, None
            if not (account.remote_username or account.remote_id or user.username):
                return account, None
            uname = (account.remote_username or user.username or "").strip()
            rid = account.remote_id or account.remote_username or uname or None
            last: AdapterResult | None = None
            for attempt in range(tries):
                adapter = get_adapter(account.panel)
                try:
                    try:
                        coro = adapter.get_usage(uname, rid, expand_links=False)  # type: ignore[call-arg]
                    except TypeError:
                        coro = adapter.get_usage(uname, rid)
                    res = await asyncio.wait_for(coro, timeout=per_try)
                    last = res
                    if res and res.success:
                        return account, res
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "get_usage exception user=%s panel=%s try=%s: %s",
                        user.username,
                        account.panel_id,
                        attempt + 1,
                        _exc_msg(exc),
                    )
                finally:
                    close = getattr(adapter, "close", None)
                    if callable(close):
                        try:
                            await close()
                        except Exception:  # noqa: BLE001
                            pass
                if attempt + 1 < tries:
                    await asyncio.sleep(0.1)
            return account, last

        # Hard overall budget — many panels must not block for minutes
        remote_accounts = [
            a for a in accounts if not (a.panel and a.panel.type == PanelType.astra)
        ]
        tasks = [asyncio.create_task(_remote(a)) for a in remote_accounts]
        done, pending = await asyncio.wait(tasks, timeout=overall_budget)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        outcomes: list[tuple[PanelAccount, AdapterResult | None]] = []
        for t in done:
            try:
                outcomes.append(t.result())
            except Exception:  # noqa: BLE001
                pass
        now = datetime.now(timezone.utc)
        from app.adapters.http_utils import is_share_link

        ok_n = 0
        for account, res in outcomes:
            if res is None:
                continue
            if res.success:
                ok_n += 1
                _note_hold_signal(user, res)
                got = int(res.used_traffic or 0)
                prev = int(account.used_traffic or 0)
                # Astra local node is authoritative (resets must shrink to 0).
                # Other remotes: never shrink on flaky/partial readings.
                panel = account.panel
                if panel is not None and panel.type == PanelType.astra:
                    account.used_traffic = got
                else:
                    account.used_traffic = max(prev, got)
                # Never replace real share links with raw /sub refs from usage pulls.
                # Never shrink a richer stored set with a partial panel response
                # (that was the main cause of fluctuating sub config counts).
                new_shares = [
                    str(x) for x in (res.links or []) if x and is_share_link(str(x))
                ]
                if new_shares:
                    old_shares = [
                        str(x)
                        for x in (account.links or [])
                        if x and is_share_link(str(x))
                    ]
                    if not old_shares or len(new_shares) >= len(old_shares):
                        account.links = new_shares
                        account.links_by_group = None
                    else:
                        logger.info(
                            "refresh_usage keep links user=%s panel=%s old=%s new=%s",
                            user.username,
                            account.panel_id,
                            len(old_shares),
                            len(new_shares),
                        )
                account.last_sync_at = now
                if account.panel:
                    account.panel.last_ok_at = now
                if account.status == PanelAccountStatus.error:
                    account.status = PanelAccountStatus.active
                    account.error_message = None
            else:
                logger.warning(
                    "get_usage failed user=%s panel=%s: %s",
                    user.username,
                    account.panel_id,
                    res.error,
                )

        logger.info(
            "refresh_usage user=%s panels=%s ok=%s force=%s quick=%s budget=%s pending=%s",
            user.username,
            len(accounts),
            ok_n,
            force,
            quick,
            overall_budget,
            len(pending),
        )

    # Refresh coeffs in case new accounts appeared (should not, but cheap)
    account_coeffs = await account_coefficients_for_user(db, user, accounts)
    total = await _weighted_total(accounts, account_coeffs)
    user.used_traffic = total
    await db.flush()
    return total


async def enforce_user_quota(db: AsyncSession, user: User) -> bool:
    """Apply expire / data-limit status. Returns True if status changed.

    When used_astra (sum of all panels × coeffs) hits data_limit → limited.
    Remotes are disabled via sync_modify even if the upstream panel still
    shows leftover traffic.
    """
    if user.status in (UserStatus.disabled, UserStatus.on_hold):
        return False

    now = int(time.time())
    old = user.status
    new = old

    expired = bool(user.expire and int(user.expire) > 0 and now >= int(user.expire))
    limited = quota_math.is_quota_exhausted(
        int(user.used_traffic or 0), int(user.data_limit or 0)
    )

    if expired:
        new = UserStatus.expired
    elif limited:
        new = UserStatus.limited
    elif old in (UserStatus.limited, UserStatus.expired):
        new = UserStatus.active

    if new == old:
        return False

    user.status = new
    await db.flush()
    logger.info(
        "quota status user=%s %s → %s used=%s limit=%s",
        user.username,
        old.value if hasattr(old, "value") else old,
        new.value if hasattr(new, "value") else new,
        int(user.used_traffic or 0),
        int(user.data_limit or 0),
    )
    return True


def _on_hold_duration_seconds(user: User) -> int:
    try:
        return max(0, int(getattr(user, "on_hold_expire_duration", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _note_hold_signal(user: User, res: AdapterResult | None) -> None:
    """Record first-connect evidence from any remote onto the user object.

    Activation itself happens in activate_on_hold_if_used; this only flags that
    Marzban / PasarGuard / Rebecca / Sanaei / Astra already started (traffic,
    online_at, or a converted positive expire).
    """
    if user.status != UserStatus.on_hold or res is None or not res.success:
        return
    st = (res.remote_status or "").strip().lower()
    exp = int(res.remote_expire or 0)
    online = int(res.online_at or 0)
    used = int(res.used_traffic or 0)
    raw = res.raw if isinstance(res.raw, dict) else {}
    if not st and raw.get("status"):
        st = str(raw.get("status") or "").strip().lower()
    if exp <= 0 and raw:
        exp = coerce_unix_ts(raw.get("expire") or raw.get("expiryTime"))
    if online <= 0 and raw:
        online = coerce_unix_ts(
            raw.get("online_at") or raw.get("onlineAt") or raw.get("last_online")
        )
    now = int(time.time())
    activated = used > 0 or online > 0
    if st in ("active", "limited", "expired") and (used > 0 or online > 0 or exp > 0):
        activated = True
    if exp > now:
        activated = True
        prev = int(getattr(user, "_hold_remote_expire", 0) or 0)
        if prev <= 0 or exp < prev:
            setattr(user, "_hold_remote_expire", exp)
    if activated:
        setattr(user, "_hold_remote_activated", True)


async def activate_on_hold_if_used(db: AsyncSession, user: User) -> bool:
    """Start the expire clock on first connect — same timestamp on every panel.

    on_hold stores duration (seconds) with expire=0. First traffic OR a remote
    that already self-activated (Marzban/PG/Rebecca online_at, Sanaei converted
    expiryTime, Astra node usage) flips status to active and sets one expire.
    A later sync_modify pushes that absolute deadline to ALL remotes so unused
    configs of this subscription start the same clock.
    """
    if user.status != UserStatus.on_hold:
        return False
    duration = _on_hold_duration_seconds(user)
    if duration <= 0:
        return False
    used = int(user.used_traffic or 0)
    if used <= 0:
        for a in user.panel_accounts or []:
            if a.status == PanelAccountStatus.deleted:
                continue
            if int(getattr(a, "used_traffic", 0) or 0) > 0:
                used = 1
                break
    remote_hit = bool(getattr(user, "_hold_remote_activated", False))
    remote_expire = 0
    try:
        remote_expire = max(0, int(getattr(user, "_hold_remote_expire", 0) or 0))
    except (TypeError, ValueError):
        remote_expire = 0
    if used <= 0 and not remote_hit and remote_expire <= 0:
        return False
    now = int(time.time())
    expire_at = now + duration
    # A remote that already converted on_hold keeps the earliest real deadline
    # so unused panels match the panel the user actually connected to.
    if remote_expire > now:
        expire_at = min(expire_at, remote_expire)
        if expire_at < now + 60:
            expire_at = now + duration
    user.status = UserStatus.active
    user.expire = expire_at
    user.on_hold_expire_duration = 0
    await db.flush()
    logger.info(
        "on_hold activate user=%s expire=%s duration=%ss remote_expire=%s used=%s",
        user.username,
        user.expire,
        duration,
        remote_expire or 0,
        used,
    )
    return True


async def sync_all_astra_traffic(db: AsyncSession) -> int:
    """Pull Xray stats for every active Astra panel into DB (same session)."""
    from app.services import astra_node as astra_node_svc

    result = await db.execute(
        select(Panel).where(
            Panel.type == PanelType.astra,
            Panel.is_active.is_(True),
        )
    )
    panels = list(result.scalars().all())
    n = 0
    for panel in panels:
        try:
            await astra_node_svc.sync_traffic_from_xray(db, panel)
            n += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "astra traffic sync failed panel=%s", panel.id, exc_info=True
            )
    await db.flush()
    return n


async def run_quota_watch_cycle() -> dict[str, int]:
    """Fast background quota: sync Astra → recount → freeze remotes immediately.

    Designed so exhausted users are disabled on ALL panels within ~10s without
    an admin opening the Astra UI.
    """
    from app.core.database import AsyncSessionLocal

    stats = {
        "astra_panels": 0,
        "checked": 0,
        "flipped": 0,
        "freeze_ok": 0,
        "freeze_fail": 0,
        "usage_pulled": 0,
    }
    flipped_ids: list[int] = []
    retry_ids: list[int] = []
    near_limit_ids: list[int] = []
    hold_ids: list[int] = []
    activate_ids: list[int] = []

    # --- Phase 1: Astra Xray counters (local, fast) ---
    async with AsyncSessionLocal() as db:
        stats["astra_panels"] = await sync_all_astra_traffic(db)
        await db.commit()

    # --- Phase 2: recount + detect flips; queue freezes ---
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User)
            .where(
                User.status.in_(
                    (
                        UserStatus.active,
                        UserStatus.on_hold,
                        UserStatus.limited,
                        UserStatus.expired,
                    )
                )
            )
            .options(
                selectinload(User.panel_accounts).selectinload(PanelAccount.panel)
            )
            .order_by(User.id.desc())
            .limit(3000)
        )
        users = list(result.scalars().unique().all())
        now_ts = time.time()
        for user in users:
            accounts = [
                a
                for a in (user.panel_accounts or [])
                if a.status
                in (
                    PanelAccountStatus.active,
                    PanelAccountStatus.error,
                    PanelAccountStatus.pending,
                )
            ]
            if not accounts:
                continue
            # Skip unlimited + no-expire users (nothing to enforce)
            has_limit = int(user.data_limit or 0) > 0
            has_expire = bool(user.expire and int(user.expire) > 0)
            waiting_hold = (
                user.status == UserStatus.on_hold
                and _on_hold_duration_seconds(user) > 0
            )
            if not has_limit and not has_expire and not waiting_hold:
                continue
            coeffs = await account_coefficients_for_user(db, user, accounts)
            user.used_traffic = await _weighted_total(accounts, coeffs)
            if waiting_hold:
                # Pull remotes first (phase 3) so we adopt the panel that
                # actually connected, then push the same expire to everyone.
                hold_ids.append(int(user.id))
                stats["checked"] += 1
                continue
            activated = await activate_on_hold_if_used(db, user)
            changed = bool(activated) or await enforce_user_quota(db, user)
            stats["checked"] += 1
            blocked = user.status in (
                UserStatus.limited,
                UserStatus.expired,
                UserStatus.disabled,
            )
            if activated:
                activate_ids.append(int(user.id))
            if changed:
                stats["flipped"] += 1
                flipped_ids.append(int(user.id))
            elif blocked:
                # Re-freeze only when last push failed or is very stale
                needs = False
                for a in accounts:
                    if a.status == PanelAccountStatus.error:
                        needs = True
                        break
                    ls = a.last_sync_at
                    if ls is None:
                        needs = True
                        break
                    try:
                        age = now_ts - ls.timestamp()
                    except Exception:  # noqa: BLE001
                        age = 9999
                    if age > 90:
                        needs = True
                        break
                if needs:
                    retry_ids.append(int(user.id))
            elif has_limit:
                lim = int(user.data_limit or 0)
                used = int(user.used_traffic or 0)
                # Near limit → pull foreign remotes this cycle for accuracy
                if lim > 0 and used >= int(lim * 0.55):
                    near_limit_ids.append(int(user.id))
        await db.commit()

    # --- Phase 3: pull foreign usage for near-limit + flipped + on_hold (quick, capped) ---
    # on_hold first — first connect on any panel must start every other clock
    pull_ids = list(dict.fromkeys(hold_ids + flipped_ids + near_limit_ids))[:60]
    for uid in pull_ids:
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(User)
                    .where(User.id == uid)
                    .options(
                        selectinload(User.panel_accounts).selectinload(
                            PanelAccount.panel
                        )
                    )
                )
                user = result.scalar_one_or_none()
                if not user:
                    continue
                await refresh_usage(db, user, force=True, quick=True)
                activated = await activate_on_hold_if_used(db, user)
                changed = bool(activated) or await enforce_user_quota(db, user)
                await db.commit()
                stats["usage_pulled"] += 1
                if activated and int(user.id) not in activate_ids:
                    activate_ids.append(int(user.id))
                if changed and int(user.id) not in flipped_ids:
                    flipped_ids.append(int(user.id))
                    stats["flipped"] += 1
        except Exception:  # noqa: BLE001
            logger.debug("near-limit usage pull failed user_id=%s", uid, exc_info=True)

    # --- Phase 4a: push absolute expire to all remotes after first use ---
    for uid in list(dict.fromkeys(activate_ids))[:40]:
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(User)
                    .where(User.id == uid)
                    .options(
                        selectinload(User.panel_accounts).selectinload(
                            PanelAccount.panel
                        )
                    )
                )
                user = result.scalar_one_or_none()
                if not user or user.status != UserStatus.active:
                    continue
                await sync_modify_user(
                    db, user, remote_timeout=12, skip_nginx_sync=True
                )
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.warning(
                "on_hold activate push failed user_id=%s", uid, exc_info=True
            )

    # --- Phase 4: freeze remotes NOW — flipped first, then a few retries ---
    freeze_ids = list(dict.fromkeys(flipped_ids + retry_ids[:15]))
    for uid in freeze_ids:
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(User)
                    .where(User.id == uid)
                    .options(
                        selectinload(User.panel_accounts).selectinload(
                            PanelAccount.panel
                        )
                    )
                )
                user = result.scalar_one_or_none()
                if not user:
                    continue
                if user.status not in (
                    UserStatus.limited,
                    UserStatus.expired,
                    UserStatus.disabled,
                ):
                    continue
                await sync_modify_user(
                    db, user, remote_timeout=10, skip_nginx_sync=True
                )
                fail = any(
                    a.status == PanelAccountStatus.error
                    for a in (user.panel_accounts or [])
                    if a.status != PanelAccountStatus.deleted
                )
                await db.commit()
                if fail:
                    stats["freeze_fail"] += 1
                else:
                    stats["freeze_ok"] += 1
        except Exception:  # noqa: BLE001
            stats["freeze_fail"] += 1
            logger.warning(
                "quota freeze failed user_id=%s", uid, exc_info=True
            )

    return stats


async def recount_group_usage_local(db: AsyncSession, group_id: int) -> list[int]:
    """Apply live GroupPanel coefficients to Astra used_traffic (no remote I/O).

    Formula: used_astra = Σ (panel_account.raw × coeff of that account's group×panel).
    Returns user ids whose quota status flipped (limited/expired/active).
    """
    ug = await db.execute(select(UserGroup.user_id).where(UserGroup.group_id == group_id))
    user_ids = [row[0] for row in ug.all()]
    if not user_ids:
        return []

    from app.services.group_configs import sample_username_for_group

    sample = sample_username_for_group(group_id)
    result = await db.execute(
        select(User)
        .where(User.id.in_(user_ids))
        .options(selectinload(User.panel_accounts))
    )
    users = [u for u in result.scalars().unique().all() if u.username != sample]
    if not users:
        return []

    ug_all = await db.execute(
        select(UserGroup).where(UserGroup.user_id.in_([int(u.id) for u in users]))
    )
    gids_by_user: dict[int, list[int]] = {}
    all_gids: set[int] = set()
    for row in ug_all.scalars().all():
        uid = int(row.user_id)
        gid = int(row.group_id)
        gids_by_user.setdefault(uid, []).append(gid)
        all_gids.add(gid)

    claims: dict[tuple[int, int], float] = {}
    if all_gids:
        gp_rows = await db.execute(
            select(GroupPanel).where(GroupPanel.group_id.in_(list(all_gids)))
        )
        for link in gp_rows.scalars().all():
            claims[(int(link.panel_id), int(link.group_id))] = coefficient_from_config(
                link.config if isinstance(link.config, dict) else {}
            )

    flipped: list[int] = []
    for user in users:
        accounts = list(user.panel_accounts or [])
        user_gids = gids_by_user.get(int(user.id), [])
        account_coeffs: dict[int, float] = {}
        for account in accounts:
            gid = getattr(account, "group_id", None)
            if gid is not None:
                account_coeffs[account.id] = _norm_coeff(
                    claims.get((int(account.panel_id), int(gid)), 1.0)
                )
            else:
                best = 1.0
                found = False
                for gg in user_gids:
                    val = claims.get((int(account.panel_id), int(gg)))
                    if val is None:
                        continue
                    if not found or val > best:
                        best = val
                        found = True
                account_coeffs[account.id] = _norm_coeff(best)
        user.used_traffic = await _weighted_total(accounts, account_coeffs)
        if await enforce_user_quota(db, user):
            flipped.append(int(user.id))
    await db.flush()
    logger.info(
        "group %s local coeff recount users=%s flipped=%s",
        group_id,
        len(users),
        len(flipped),
    )
    return flipped


async def resync_users_for_group(
    db: AsyncSession,
    group_id: int,
    *,
    removed_panel_ids: list[int] | None = None,
) -> int:
    """After group panel membership / coefficient / config change.

    For every user (and sample probe) in the sales group:

    1. Delete upstream accounts for panels no longer linked to this group
       (remove from panel + Astra DB — not only from the sub feed).
    2. Provision newly added panels and hydrate share links.
    3. Recount weighted usage and push remotes (caps / disable on quota).

    Local recount should already have run on save for immediate UI feedback.
    """
    from app.models.group import Group
    from app.models.group_config import GroupConfigItem
    from app.services.group_configs import (
        _get_sample_user,
        sample_username_for_group,
        sync_items_from_sample,
    )
    from sqlalchemy import delete

    gid = int(group_id)
    removed_hint = {int(p) for p in (removed_panel_ids or []) if p is not None}
    # Membership = all GroupPanel rows (even inactive panels keep remotes until
    # the admin actually removes the panel from the group).
    gp_ids = await db.execute(
        select(GroupPanel.panel_id).where(GroupPanel.group_id == gid)
    )
    desired_panel_ids = {int(pid) for (pid,) in gp_ids.all() if pid is not None}
    # Provision only active panels (same as create-user / heal).
    targets = await resolve_panels_for_groups(db, [gid])

    # Drop admin config rows whose panel left this group
    if desired_panel_ids:
        await db.execute(
            delete(GroupConfigItem).where(
                GroupConfigItem.group_id == gid,
                GroupConfigItem.panel_id.notin_(desired_panel_ids),
            )
        )
    else:
        await db.execute(
            delete(GroupConfigItem).where(GroupConfigItem.group_id == gid)
        )
    await db.flush()

    async def _reconcile_user(user: User) -> None:
        # Reload accounts with panels
        acc_q = await db.execute(
            select(PanelAccount)
            .where(PanelAccount.user_id == user.id)
            .options(selectinload(PanelAccount.panel))
        )
        accounts = list(acc_q.scalars().all())
        user.panel_accounts = accounts

        ug_all = await db.execute(
            select(UserGroup.group_id).where(UserGroup.user_id == user.id)
        )
        user_gids = [int(r[0]) for r in ug_all.all() if r[0] is not None]
        still_claimed: set[int] = set()
        if user_gids:
            claim_q = await db.execute(
                select(GroupPanel.panel_id).where(GroupPanel.group_id.in_(user_gids))
            )
            still_claimed = {
                int(pid) for (pid,) in claim_q.all() if pid is not None
            }

        # Stale for THIS group: tagged with gid and panel left the group
        stale: list[PanelAccount] = []
        seen_acc: set[int] = set()
        for a in accounts:
            if a.status == PanelAccountStatus.deleted:
                continue
            aid = int(a.id)
            pid = int(a.panel_id)
            agid = getattr(a, "group_id", None)
            # Per-group remote for this sales group, panel unlinked
            if agid is not None and int(agid) == gid and pid not in desired_panel_ids:
                stale.append(a)
                seen_acc.add(aid)
                continue
            # Explicitly removed panel ids (hint from group save)
            if pid in removed_hint and pid not in still_claimed and aid not in seen_acc:
                stale.append(a)
                seen_acc.add(aid)
                continue
            # Legacy NULL group_id orphan
            if agid is None and pid not in still_claimed and aid not in seen_acc:
                stale.append(a)
                seen_acc.add(aid)

        if stale:
            await _delete_panel_accounts_remote(db, user, stale, clear_links_always=True)
            # Force deleted when panel is no longer claimed by any of user's groups
            for a in stale:
                if int(a.panel_id) not in still_claimed:
                    a.status = PanelAccountStatus.deleted
                    a.links = []
                    a.links_by_group = {}
            await db.flush()

        if targets:
            await provision_user_on_panels(db, user, targets)
            fresh_acc = await db.execute(
                select(PanelAccount)
                .where(PanelAccount.user_id == user.id)
                .options(selectinload(PanelAccount.panel))
            )
            user.panel_accounts = list(fresh_acc.scalars().all())
            try:
                await hydrate_share_links(db, user)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "group %s hydrate user=%s failed",
                    gid,
                    user.username,
                    exc_info=True,
                )

    # Sample probe first — keeps Configs page + hide rules panel-aligned
    sample_name = sample_username_for_group(gid)
    sample = await _get_sample_user(db, gid)
    if sample is not None:
        try:
            await _reconcile_user(sample)
            sample = await _get_sample_user(db, gid)
            group = await db.get(Group, gid)
            if sample is not None and group is not None:
                await sync_items_from_sample(db, group, sample)
        except Exception:  # noqa: BLE001
            logger.exception("group %s sample reconcile failed", gid)

    ug = await db.execute(select(UserGroup.user_id).where(UserGroup.group_id == gid))
    user_ids = [row[0] for row in ug.all()]
    if not user_ids:
        await db.flush()
        return 0

    result = await db.execute(
        select(User)
        .where(User.id.in_(user_ids))
        .options(selectinload(User.panel_accounts).selectinload(PanelAccount.panel))
    )
    users = [
        u
        for u in result.scalars().unique().all()
        if u.username != sample_name
        and not str(u.username or "").startswith(f"ag{gid}s")
        and "نمونه کانفیگ" not in (u.note or "")
    ]
    if not users:
        await db.flush()
        return 0

    ok = 0
    for user in users:
        try:
            await _reconcile_user(user)
            ok += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "group %s reconcile user=%s failed",
                gid,
                getattr(user, "username", "?"),
            )

    # Re-apply coeffs from DB raw, then pull remotes + push caps
    await recount_group_usage_local(db, gid)
    # Reload users after reconcile (accounts may have changed)
    result2 = await db.execute(
        select(User)
        .where(User.id.in_([u.id for u in users]))
        .options(selectinload(User.panel_accounts).selectinload(PanelAccount.panel))
    )
    fresh_users = list(result2.scalars().unique().all())
    if fresh_users:
        await refresh_users_usage_batch(
            db, fresh_users, force=True, concurrency=8, push_remotes=True
        )
    await db.flush()
    logger.info(
        "group %s panel/coeff resync: reconciled=%s/%s desired_panels=%s",
        gid,
        ok,
        len(users),
        sorted(desired_panel_ids),
    )
    return ok


async def sync_subscription_state(
    db: AsyncSession,
    user: User,
    *,
    force: bool = False,
    push_remotes: bool | None = None,
) -> User:
    """Usage refresh + shared-quota enforce + optional push to remotes.

    Safe under concurrent sub clients via per-user lock.

    push_remotes:
      None  → auto (blocked / multi-panel / status change / errors)
      True  → always push remaining caps
      False → never push (usage read only) — still discouraged when blocked
    """
    lock = await _lock_for_user(user.id)
    async with lock:
        await refresh_usage(db, user, force=force)
        changed = await enforce_user_quota(db, user)
        result = await db.execute(
            select(PanelAccount).where(PanelAccount.user_id == user.id)
        )
        accounts = list(result.scalars().all())
        has_err = any(a.status == PanelAccountStatus.error for a in accounts)
        if push_remotes is True:
            do_push = True
        elif push_remotes is False:
            # Still disable remotes when Astra quota is finished
            blocked = user.status in (
                UserStatus.limited,
                UserStatus.expired,
                UserStatus.disabled,
            )
            do_push = bool(changed or blocked)
        else:
            do_push = has_err or await should_push_remotes(
                db,
                user,
                accounts,
                status_changed=changed,
                force=False,
                shrink_shared_caps=True,
            )
        if do_push:
            await sync_modify_user(db, user)
        await db.flush()
        return user


async def heal_panel_after_enable(panel_id: int) -> dict:
    """
    After an Astra panel is turned back on:

    1. Find every sales group that still links this panel.
    2. Provision this panel for users (and sample probes) in those groups
       when the remote account is missing / not active.
    3. Hydrate share links so old + new subs show configs again.

    Runs in a fresh DB session (background task). Does not wipe hide rules.
    """
    from app.core.database import AsyncSessionLocal
    from app.models.group import Group, GroupPanel, UserGroup
    from app.services.group_configs import (
        _get_sample_user,
        sample_username_for_group,
        sync_items_from_sample,
    )

    stats = {"groups": 0, "users": 0, "samples": 0, "errors": 0}
    try:
        async with AsyncSessionLocal() as db:
            panel = await db.get(Panel, panel_id)
            if not panel or not panel.is_active:
                return stats

            gp = await db.execute(
                select(GroupPanel.group_id).where(GroupPanel.panel_id == panel_id)
            )
            group_ids = sorted({int(r[0]) for r in gp.all() if r[0] is not None})
            stats["groups"] = len(group_ids)
            if not group_ids:
                return stats

            for gid in group_ids:
                try:
                    targets = await resolve_panels_for_groups(db, [gid])
                    panel_targets = [
                        t for t in targets if int(t[0].id) == int(panel_id)
                    ]
                    if not panel_targets:
                        continue

                    # Sample probe first — keeps admin config list in sync
                    sample = await _get_sample_user(db, gid)
                    if sample is not None:
                        await provision_user_on_panels(db, sample, panel_targets)
                        sample = await _get_sample_user(db, gid)
                        if sample is not None:
                            group = await db.get(Group, gid)
                            if group is not None:
                                await sync_items_from_sample(db, group, sample)
                        stats["samples"] += 1

                    ug = await db.execute(
                        select(UserGroup.user_id).where(UserGroup.group_id == gid)
                    )
                    user_ids = [int(r[0]) for r in ug.all() if r[0] is not None]
                    if not user_ids:
                        await db.commit()
                        continue

                    sample_name = sample_username_for_group(gid)
                    result = await db.execute(
                        select(User)
                        .where(User.id.in_(user_ids))
                        .options(
                            selectinload(User.panel_accounts).selectinload(
                                PanelAccount.panel
                            )
                        )
                    )
                    users = [
                        u
                        for u in result.scalars().unique().all()
                        if u.username != sample_name
                        and not str(u.username or "").startswith(f"ag{gid}s")
                        and "نمونه کانفیگ" not in (u.note or "")
                    ]

                    for user in users:
                        try:
                            await provision_user_on_panels(db, user, panel_targets)
                            # Reload accounts onto user for hydrate
                            acc = await db.execute(
                                select(PanelAccount)
                                .where(PanelAccount.user_id == user.id)
                                .options(selectinload(PanelAccount.panel))
                            )
                            user.panel_accounts = list(acc.scalars().all())
                            await hydrate_share_links(db, user)
                            stats["users"] += 1
                        except Exception:  # noqa: BLE001
                            stats["errors"] += 1
                            logger.exception(
                                "heal panel=%s user=%s failed",
                                panel_id,
                                getattr(user, "username", "?"),
                            )
                            try:
                                await db.rollback()
                            except Exception:  # noqa: BLE001
                                pass
                            # re-fetch panel for next iterations after rollback
                            panel = await db.get(Panel, panel_id)
                            if not panel or not panel.is_active:
                                return stats

                    await db.commit()
                except Exception:  # noqa: BLE001
                    stats["errors"] += 1
                    logger.exception(
                        "heal panel=%s group=%s failed", panel_id, gid
                    )
                    try:
                        await db.rollback()
                    except Exception:  # noqa: BLE001
                        pass

            logger.info(
                "heal panel=%s done groups=%s users=%s samples=%s errors=%s",
                panel_id,
                stats["groups"],
                stats["users"],
                stats["samples"],
                stats["errors"],
            )
    except Exception:  # noqa: BLE001
        logger.exception("heal panel=%s fatal", panel_id)
        stats["errors"] += 1
    return stats
