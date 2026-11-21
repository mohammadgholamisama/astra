import asyncio
import logging
import re
import secrets
import time
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
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


def bulk_retry_in_progress() -> bool:
    """True while admin/auto retry-all holds the global lock."""
    return _bulk_retry_lock.locked()
# One group panel-resync at a time per group (manual Apply + auto after save)
_group_resync_locks: dict[int, asyncio.Lock] = {}
_group_resync_running: set[int] = set()
# If admin changes panels mid-run: cancel current wave and re-apply latest membership
_group_resync_cancel: set[int] = set()
_group_resync_dirty_removed: dict[int, set[int]] = {}


def _group_resync_lock(group_id: int) -> asyncio.Lock:
    gid = int(group_id)
    lock = _group_resync_locks.get(gid)
    if lock is None:
        lock = asyncio.Lock()
        _group_resync_locks[gid] = lock
    return lock


def group_resync_in_progress(group_id: int) -> bool:
    return int(group_id) in _group_resync_running


def mark_group_resync_dirty(
    group_id: int, removed_panel_ids: list[int] | None = None
) -> None:
    """Queue a re-run with latest panel membership (e.g. panel removed mid-apply)."""
    gid = int(group_id)
    bucket = _group_resync_dirty_removed.setdefault(gid, set())
    for raw in removed_panel_ids or []:
        try:
            bucket.add(int(raw))
        except (TypeError, ValueError):
            continue
    _group_resync_cancel.add(gid)
    try:
        from app.services.ops_progress import update_job
        from app.services import ops_progress as _op

        jid = _op._group_jobs.get(gid)
        if jid:
            update_job(
                jid,
                message=(
                    "تغییر جدید گروه ثبت شد — این موج قطع می‌شود "
                    "و با آخرین پنل‌ها از نو روی ساب اعمال می‌شود…"
                ),
            )
    except Exception:  # noqa: BLE001
        pass


def group_resync_should_cancel(group_id: int) -> bool:
    return int(group_id) in _group_resync_cancel


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


def _is_timeout_err(err: str | None) -> bool:
    """Operation timed out — panel may still be up (slow / busy)."""
    low = (err or "").lower()
    return any(
        x in low
        for x in (
            "timeout",
            "timed out",
            "time out",
            "زمان تمام",
            "پاسخ نداد",
            "waiting for panel response",
        )
    )


def _is_unreachable_err(err: str | None) -> bool:
    """Network / timeout / DNS — remote may be slow or briefly unreachable."""
    if _is_timeout_err(err):
        return True
    low = (err or "").lower()
    return any(
        x in low
        for x in (
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
        )
    )


def _is_auth_err(err: str | None) -> bool:
    """Wrong panel username/password — retries will never succeed until fixed."""
    low = (err or "").lower()
    return any(
        x in low
        for x in (
            "incorrect username or password",
            "wrong password",
            "invalid password",
            "invalid credentials",
            "authentication failed",
            "login failed",
            "unauthorized",
            "401",
            "رمز عبور",
            "رمز اشتباه",
            "نام کاربری یا رمز",
        )
    )


def _is_hard_down_err(err: str | None) -> bool:
    """Clear permanent/unreachable failure — safe to skip without retrying accounts."""
    if _is_auth_err(err):
        return True
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
            "forbidden",
            "403",
        )
    )


def _short_panel_down_reason(err: str | None) -> str:
    """One-line Persian reason for banner / toast."""
    if _is_auth_err(err):
        return "نام کاربری یا رمز پنل اشتباه است"
    if _is_unreachable_err(err):
        return "پنل در دسترس نیست (قطع / تایم‌اوت)"
    detail = (err or "").strip()
    return detail[:80] if detail else "پنل پاسخ درست نمی‌دهد"


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
    if _is_auth_err(detail):
        parts.append(
            "علت: نام کاربری یا رمز ورود این پنل در Astra اشتباه است. "
            "تا در تنظیمات پنل درست نشود، خطاهای این پنل با تلاش مجدد رفع نمی‌شوند."
        )
        parts.append(f"پاسخ پنل: {detail}")
    elif _is_username_taken_err(detail):
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
    elif _is_timeout_err(detail):
        parts.append(
            "علت: این درخواست طول کشید و تایم‌اوت شد — لزوماً یعنی پنل قطع نیست "
            "(ممکن است کند/شلوغ باشد). تلاش مجدد خودکار دوباره می‌سازد."
        )
        parts.append(f"پاسخ پنل: {detail}")
    elif _is_unreachable_err(detail):
        parts.append(
            "علت: ارتباط با پنل برقرار نشد (شبکه). "
            "اگر پنل از جای دیگر باز می‌شود، ممکن است فقط از سرور Astra موقتاً نرسد؛ "
            "تلاش مجدد خودکار ادامه می‌دهد."
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


def _assign_user_used_traffic(
    user: User, total: int, *, allow_shrink: bool = False
) -> int:
    """Write ``user.used_traffic``.

    By default usage is monotonic: removing a panel/config must never refund
    volume the user already burned. Pass ``allow_shrink=True`` only for
    intentional admin resets.
    """
    new = max(0, int(total or 0))
    if allow_shrink:
        user.used_traffic = new
        return new
    cur = int(user.used_traffic or 0)
    if new < cur:
        return cur
    user.used_traffic = new
    return new


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
    """Remote ceiling = used_raw + remaining Astra budget / coefficient."""
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

    Active: each panel gets used_raw + remaining_astra/coeff (self-cuts near sale end).
    Limited/expired/disabled: freeze at current raw + disabled status on ALL panels.
    Coefficient also drives Astra usage Σ(raw×coeff).

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
        # Guard API uses days; adapters that need seconds keep on_hold_expire_duration
        cfg["on_hold_timeout_days"] = max(1, min(365, (hold_dur + 86399) // 86400))
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
    skip_astra_rebuild: bool = False,
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

    ``skip_astra_rebuild=True``: leave Xray/nginx rebuild to the caller (e.g. one
    rebuild per panel after a whole group resync wave).
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

    # Astra local: skip per-user xray/nginx rebuild during the parallel wave —
    # 1000+ clients × N concurrent rebuilds caused «Timeout waiting for panel
    # response» on Out* panels. One rebuild per panel after the wave.
    astra_panel_ids: set[int] = set()
    for i, (account, panel, payload, gid) in enumerate(jobs):
        if panel.type != PanelType.astra:
            continue
        astra_panel_ids.add(int(panel.id))
        cfg = dict(payload.config or {})
        cfg["_astra_skip_nginx"] = True
        cfg["_astra_skip_rebuild"] = True
        payload.config = cfg
        jobs[i] = (account, panel, payload, gid)

    async def _remote(
        panel: Panel, payload: UserPayload, *, prefer_modify: bool
    ) -> AdapterResult:
        adapter = get_adapter(panel)
        t0 = time.monotonic()
        # Local Astra upsert is DB-only when rebuild is deferred.
        panel_timeout = (
            max(float(create_timeout), 35.0)
            if panel.type == PanelType.astra
            else float(create_timeout)
        )
        try:
            async with sem:
                if prefer_modify:
                    try:
                        mod = await asyncio.wait_for(
                            adapter.modify_user(payload), timeout=panel_timeout
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
                    timeout=panel_timeout,
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
            got = int(result.used_traffic or 0)
            prev = int(account.used_traffic or 0)
            # Re-adding a retired config must not refund Astra volume
            account.used_traffic = max(prev, got)
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
        elif _is_timeout_err(result.error) or (
            _is_unreachable_err(result.error) and not _is_hard_down_err(result.error)
        ):
            # Panel may be up but slow — don't sticky-banner as «قطع»
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

    # One Xray+nginx rebuild per Astra panel touched in this provision wave.
    # Callers that batch many users (group resync) pass skip_astra_rebuild and
    # rebuild once after the wave instead of N×panels times.
    if (
        not skip_astra_rebuild
        and astra_panel_ids
        and any(
            a.status == PanelAccountStatus.active and int(a.panel_id) in astra_panel_ids
            for a in accounts
        )
    ):
        try:
            from app.core.database import AsyncSessionLocal
            from app.services import astra_node as astra_node_svc

            async with AsyncSessionLocal() as sdb:
                for pid in sorted(astra_panel_ids):
                    panel_row = await sdb.get(Panel, int(pid))
                    if panel_row is None:
                        continue
                    try:
                        await astra_node_svc.rebuild_xray_config(
                            sdb, panel_row, sync_nginx=False
                        )
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "astra post-provision rebuild panel=%s failed",
                            pid,
                            exc_info=True,
                        )
                try:
                    await astra_node_svc.sync_nginx_ws_upstreams(sdb)
                except Exception:  # noqa: BLE001
                    logger.debug("astra post-provision nginx sync failed", exc_info=True)
                await sdb.commit()
        except Exception:  # noqa: BLE001
            logger.warning(
                "astra post-provision rebuild user=%s failed",
                user.username,
                exc_info=True,
            )

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


async def provision_users_batch(
    db: AsyncSession,
    users: list[User],
    panels: list[tuple[Panel, dict] | tuple[Panel, dict, int]],
    *,
    create_timeout: float = 10.0,
    concurrency: int = 40,
    overall_timeout: float | None = None,
) -> dict[str, int]:
    """Provision many users × all group/panel targets in one parallel wave.

    Local PanelAccount rows are prepared in a single commit, then every remote
    create runs concurrently. Hydrate is skipped (sub/heal fills links).
    Returns {jobs, ok, err, pending}.
    """
    targets = _normalize_panel_targets(panels)
    if not users or not targets:
        return {"jobs": 0, "ok": 0, "err": 0, "pending": 0}

    t0 = time.monotonic()
    jobs: list[tuple[PanelAccount, Panel, UserPayload, User]] = []
    panel_counts: dict[int, int] = {}
    for p, _, gid in targets:
        if gid:
            panel_counts[p.id] = panel_counts.get(p.id, 0) + 1

    user_ids = [int(u.id) for u in users]
    existing_rows = await db.execute(
        select(PanelAccount).where(PanelAccount.user_id.in_(user_ids))
    )
    existing_by: dict[tuple[int, int, int], PanelAccount] = {}
    for a in existing_rows.scalars().all():
        gid = int(getattr(a, "group_id", 0) or 0)
        existing_by[(int(a.user_id), int(a.panel_id), gid)] = a

    created_any = False
    for user in users:
        uid = int(user.id)
        for panel, config, group_id in targets:
            gid = int(group_id or 0)
            key = (uid, int(panel.id), gid)
            account = existing_by.get(key)
            if account is None:
                account = PanelAccount(
                    user_id=uid,
                    panel_id=panel.id,
                    group_id=gid or None,
                    status=PanelAccountStatus.pending,
                )
                db.add(account)
                existing_by[key] = account
                created_any = True
            elif account.status == PanelAccountStatus.active:
                continue
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
            jobs.append((account, panel, payload, user))

    if created_any:
        await db.flush()
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        pass
    if not jobs:
        return {"jobs": 0, "ok": 0, "err": 0, "pending": 0}

    # Same as provision_user_on_panels: defer Astra xray/nginx rebuild until
    # after the wave so mass Out* creates do not stampede reloads.
    astra_panel_ids: set[int] = set()
    for i, (account, panel, payload, user) in enumerate(jobs):
        if panel.type != PanelType.astra:
            continue
        astra_panel_ids.add(int(panel.id))
        cfg = dict(payload.config or {})
        cfg["_astra_skip_nginx"] = True
        cfg["_astra_skip_rebuild"] = True
        payload.config = cfg
        jobs[i] = (account, panel, payload, user)

    n_jobs = len(jobs)
    sem_limit = max(32, min(int(concurrency) or 48, 64, n_jobs))
    waves = max(1, (n_jobs + sem_limit - 1) // sem_limit)
    budget = (
        float(overall_timeout)
        if overall_timeout is not None
        else min(28.0, waves * min(create_timeout, 5.0) + 3.0)
    )
    sem = asyncio.Semaphore(sem_limit)

    async def _remote(
        panel: Panel, payload: UserPayload, user: User
    ) -> AdapterResult:
        adapter = get_adapter(panel)
        panel_timeout = (
            max(float(create_timeout), 20.0)
            if panel.type == PanelType.astra
            else float(create_timeout)
        )
        try:
            async with sem:
                return await asyncio.wait_for(
                    create_user_with_collision_alias(
                        adapter,
                        payload,
                        astra_username=user.username,
                        panel_id=panel.id,
                        timeout=panel_timeout,
                        max_alias_attempts=1,
                        probe_timeout=2.0,
                    ),
                    timeout=panel_timeout + 0.8,
                )
        except asyncio.CancelledError:
            return _fail_result(_PROVISION_BUDGET_SOFT)
        except Exception as exc:  # noqa: BLE001
            return _fail_result(_exc_msg(exc) or "خطای پنل")
        finally:
            try:
                await adapter.close()  # type: ignore[attr-defined]
            except Exception:
                pass

    tasks = [
        asyncio.create_task(_remote(panel, payload, user))
        for account, panel, payload, user in jobs
    ]
    logger.info(
        "bulk provision start users=%s jobs=%s sem=%s budget=%.1fs",
        len(users),
        n_jobs,
        sem_limit,
        budget,
    )
    done, pending = await asyncio.wait(tasks, timeout=budget)

    now = datetime.now(timezone.utc)
    ok_n = 0
    err_n = 0
    pend_n = 0
    leftover: list[tuple[int, asyncio.Task]] = []
    leftover_uids: set[int] = set()
    for (account, panel, payload, user), task in zip(jobs, tasks):
        if task not in done:
            account.status = PanelAccountStatus.pending
            account.remote_username = (
                account.remote_username or payload.username or user.username
            )
            account.error_message = None
            pend_n += 1
            leftover.append((int(account.id), task))
            leftover_uids.add(int(user.id))
            continue
        try:
            result = task.result()
        except Exception as exc:  # noqa: BLE001
            result = _fail_result(_exc_msg(exc) or "خطای پنل")
        if result.success:
            account.status = PanelAccountStatus.active
            account.remote_username = (
                result.remote_username or payload.username or user.username
            )
            account.remote_id = result.remote_id or account.remote_id
            account.uuid = result.uuid or account.uuid
            if result.links:
                account.links = result.links
            got = int(result.used_traffic or 0)
            prev = int(account.used_traffic or 0)
            account.used_traffic = max(prev, got)
            account.error_message = None
            account.last_sync_at = now
            panel.last_ok_at = now
            ok_n += 1
        else:
            account.status = PanelAccountStatus.error
            account.error_message = _format_remote_error(
                panel,
                action="ساخت کاربر",
                err=result.error or "ایجاد در پنل ناموفق",
                username=payload.username,
            )
            err_n += 1

    await db.flush()
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        pass

    if astra_panel_ids and ok_n:
        try:
            from app.core.database import AsyncSessionLocal
            from app.services import astra_node as astra_node_svc

            async with AsyncSessionLocal() as sdb:
                for pid in sorted(astra_panel_ids):
                    panel_row = await sdb.get(Panel, int(pid))
                    if panel_row is None:
                        continue
                    try:
                        await astra_node_svc.rebuild_xray_config(
                            sdb, panel_row, sync_nginx=False
                        )
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "astra post-batch rebuild panel=%s failed",
                            pid,
                            exc_info=True,
                        )
                try:
                    await astra_node_svc.sync_nginx_ws_upstreams(sdb)
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "astra post-batch nginx sync failed", exc_info=True
                    )
                await sdb.commit()
        except Exception:  # noqa: BLE001
            logger.warning("astra post-batch rebuild failed", exc_info=True)

    if leftover:
        asyncio.create_task(
            _finish_leftover_provision(leftover, leftover_uids),
            name="astra-prov-leftover",
        )
    logger.info(
        "bulk provision wave users=%s jobs=%s ok=%s err=%s leftover=%s dt=%.2fs",
        len(users),
        n_jobs,
        ok_n,
        err_n,
        pend_n,
        time.monotonic() - t0,
    )
    return {"jobs": n_jobs, "ok": ok_n, "err": err_n, "pending": pend_n}


async def _finish_leftover_provision(
    leftover: list[tuple[int, asyncio.Task]],
    user_ids: set[int],
) -> None:
    """Write each leftover result as soon as it finishes so the sub updates live."""
    from app.core.database import AsyncSessionLocal
    from app.api.subscription import invalidate_user_feed_cache

    if not leftover:
        return

    async def _write_one(account_id: int, task: asyncio.Task) -> None:
        try:
            raw = await task
        except Exception as exc:  # noqa: BLE001
            raw = exc
        try:
            async with AsyncSessionLocal() as db:
                acc = await db.get(PanelAccount, account_id)
                if acc is None or acc.status == PanelAccountStatus.deleted:
                    return
                uid = int(acc.user_id)
                if isinstance(raw, Exception):
                    acc.status = PanelAccountStatus.error
                    acc.error_message = _exc_msg(raw)[:300]
                else:
                    result = (
                        raw if isinstance(raw, AdapterResult) else _fail_result("خطای پنل")
                    )
                    if result.success:
                        now = datetime.now(timezone.utc)
                        acc.status = PanelAccountStatus.active
                        acc.remote_username = (
                            result.remote_username or acc.remote_username
                        )
                        acc.remote_id = result.remote_id or acc.remote_id
                        acc.uuid = result.uuid or acc.uuid
                        if result.links:
                            acc.links = result.links
                        acc.used_traffic = max(
                            int(acc.used_traffic or 0), int(result.used_traffic or 0)
                        )
                        acc.error_message = None
                        acc.last_sync_at = now
                    else:
                        acc.status = PanelAccountStatus.error
                        acc.error_message = (result.error or "ایجاد در پنل ناموفق")[:400]
                await db.commit()
                try:
                    invalidate_user_feed_cache(uid)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            logger.exception("leftover write account=%s failed", account_id)

    await asyncio.gather(
        *[_write_one(aid, t) for aid, t in leftover],
        return_exceptions=True,
    )
    logger.info("leftover provision done n=%s users=%s", len(leftover), len(user_ids))


async def probe_unique_panels(
    panels: list[Panel],
    *,
    timeout: float = 3.2,
) -> dict[int, tuple[str, str]]:
    """Parallel live-check. Astra local is always ok. Returns id → (ok|soft|down, err)."""
    uniq: dict[int, Panel] = {}
    for p in panels:
        if p is None:
            continue
        uniq[int(p.id)] = p
    if not uniq:
        return {}

    async def _one(panel: Panel) -> tuple[int, str, str]:
        if panel.type == PanelType.astra:
            return int(panel.id), "ok", ""
        state, err = await _probe_panel_alive(panel, timeout)
        return int(panel.id), state, err

    tasks = [asyncio.create_task(_one(p)) for p in uniq.values()]
    done, pending = await asyncio.wait(tasks, timeout=timeout + 0.8)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    out: dict[int, tuple[str, str]] = {
        pid: ("down", "timeout تست اتصال") for pid in uniq
    }
    for t in done:
        try:
            pid, state, err = t.result()
            out[int(pid)] = (state, err or "")
        except Exception:  # noqa: BLE001
            pass
    return out


_account_prov_sem: asyncio.Semaphore | None = None


def _get_account_prov_sem() -> asyncio.Semaphore:
    """Cap concurrent creates — each used to hold a DB checkout during HTTP.

    48 parallel sessions starved QueuePool (32+40) → /sub «Temporarily unavailable».
    Keep well under pool capacity so login/API/sub always have headroom.
    """
    global _account_prov_sem
    if _account_prov_sem is None:
        _account_prov_sem = asyncio.Semaphore(8)
    return _account_prov_sem


def _touch_panel_for_detach(panel: Panel) -> Panel:
    """Force-load attrs so get_adapter works after the session closes."""
    _ = (
        panel.id,
        panel.type,
        panel.name,
        panel.is_active,
        getattr(panel, "base_url", None),
        getattr(panel, "url", None),
        getattr(panel, "username", None),
        getattr(panel, "password", None),
        getattr(panel, "token", None),
        getattr(panel, "meta", None),
    )
    return panel


async def _provision_account_now(account_id: int) -> bool:
    """Create one remote account without holding a pool connection during HTTP.

    Load → close session → remote create/expand → short apply session.
    Never deletes rows; only updates status/links on the existing account.
    """
    from app.adapters.http_utils import (
        expand_subscription_links,
        is_share_link,
        is_subscription_ref,
        make_client,
    )
    from app.api.subscription import invalidate_user_feed_cache
    from app.core.database import AsyncSessionLocal

    async with _get_account_prov_sem():
        try:
            job: dict | None = None
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(PanelAccount)
                    .where(PanelAccount.id == int(account_id))
                    .options(
                        selectinload(PanelAccount.panel),
                        selectinload(PanelAccount.user),
                    )
                )
                acc = result.scalar_one_or_none()
                if acc is None or acc.status == PanelAccountStatus.deleted:
                    return False
                if acc.status == PanelAccountStatus.active and (acc.links or []):
                    return True
                user = acc.user
                panel = acc.panel
                uid = int(acc.user_id)
                if user is None or panel is None or not bool(
                    getattr(panel, "is_active", True)
                ):
                    acc.status = PanelAccountStatus.error
                    acc.error_message = "پنل در دسترس نیست"
                    await db.commit()
                    invalidate_user_feed_cache(uid)
                    return False
                cfg: dict = {}
                gid = int(getattr(acc, "group_id", 0) or 0)
                if gid:
                    gp = (
                        await db.execute(
                            select(GroupPanel).where(
                                GroupPanel.group_id == gid,
                                GroupPanel.panel_id == int(acc.panel_id),
                            )
                        )
                    ).scalar_one_or_none()
                    if gp is not None:
                        cfg = dict(gp.config or {})
                same_panel = 0
                if gid:
                    n = await db.execute(
                        select(func.count())
                        .select_from(PanelAccount)
                        .where(
                            PanelAccount.user_id == uid,
                            PanelAccount.panel_id == int(acc.panel_id),
                            PanelAccount.status != PanelAccountStatus.deleted,
                        )
                    )
                    same_panel = int(n.scalar() or 0)
                existing_name = (acc.remote_username or "").strip()
                if existing_name:
                    preferred = existing_name
                elif same_panel > 1 and gid:
                    preferred = make_group_remote_username(user.username, gid)
                else:
                    preferred = (user.username or "").strip()
                payload = build_user_payload(
                    user,
                    uuid=acc.uuid,
                    panel_config=cfg,
                    remote_username=preferred,
                )
                _touch_panel_for_detach(panel)
                job = {
                    "account_id": int(acc.id),
                    "user_id": uid,
                    "panel": panel,
                    "payload": payload,
                    "astra_username": str(user.username or ""),
                    "remote_id": acc.remote_id,
                    "prev_used": int(acc.used_traffic or 0),
                }
                await db.commit()
            # Session closed → connection returned before remote HTTP.

            assert job is not None
            panel = job["panel"]
            payload = job["payload"]
            uid = int(job["user_id"])
            adapter = get_adapter(panel)
            try:
                res = await asyncio.wait_for(
                    create_user_with_collision_alias(
                        adapter,
                        payload,
                        astra_username=job["astra_username"],
                        panel_id=panel.id,
                        timeout=5.0,
                        max_alias_attempts=1,
                        probe_timeout=2.0,
                    ),
                    timeout=6.0,
                )
            except Exception as exc:  # noqa: BLE001
                res = _fail_result(_exc_msg(exc) or "خطای پنل")
            finally:
                try:
                    await adapter.close()  # type: ignore[attr-defined]
                except Exception:
                    pass

            links: list[str] = []
            if res.success:
                links = [str(x).strip() for x in (res.links or []) if x]
                if links and (
                    any(is_subscription_ref(x) for x in links)
                    or not any(is_share_link(x) for x in links)
                ):
                    try:
                        client = make_client(
                            panel.base_url,
                            bool((panel.meta or {}).get("verify_ssl", False)),
                            timeout=8.0,
                            xhr=False,
                        )
                        try:
                            expanded = await asyncio.wait_for(
                                expand_subscription_links(client, links),
                                timeout=6.0,
                            )
                        finally:
                            await client.aclose()
                        shares = [x for x in (expanded or []) if is_share_link(x)]
                        if shares:
                            links = shares
                    except Exception:  # noqa: BLE001
                        pass
                if not any(is_share_link(x) for x in links):
                    adapter2 = get_adapter(panel)
                    try:
                        extra = await asyncio.wait_for(
                            adapter2.get_usage(  # type: ignore[call-arg]
                                payload.username,
                                job.get("remote_id") or payload.username,
                                expand_links=True,
                            ),
                            timeout=8.0,
                        )
                        if extra.success:
                            shares = [
                                str(x).strip()
                                for x in (extra.links or [])
                                if x and is_share_link(str(x))
                            ]
                            if shares:
                                links = shares
                    except TypeError:
                        pass
                    except Exception:  # noqa: BLE001
                        pass
                    finally:
                        try:
                            await adapter2.close()  # type: ignore[attr-defined]
                        except Exception:
                            pass

            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(PanelAccount)
                    .where(PanelAccount.id == int(job["account_id"]))
                    .options(selectinload(PanelAccount.panel))
                )
                acc = result.scalar_one_or_none()
                if acc is None or acc.status == PanelAccountStatus.deleted:
                    return False
                now = datetime.now(timezone.utc)
                panel_row = acc.panel
                if res.success:
                    acc.status = PanelAccountStatus.active
                    acc.remote_username = (
                        res.remote_username
                        or payload.username
                        or job["astra_username"]
                    )
                    acc.remote_id = res.remote_id or acc.remote_id
                    acc.uuid = res.uuid or acc.uuid
                    if links:
                        acc.links = links
                    acc.used_traffic = max(
                        int(acc.used_traffic or 0),
                        int(job["prev_used"]),
                        int(res.used_traffic or 0),
                    )
                    acc.error_message = None
                    acc.last_sync_at = now
                    if panel_row is not None:
                        panel_row.last_ok_at = now
                    await db.commit()
                    invalidate_user_feed_cache(uid)
                    return True
                acc.status = PanelAccountStatus.error
                acc.error_message = _format_remote_error(
                    panel_row or panel,
                    action="ساخت کاربر",
                    err=res.error or "ایجاد در پنل ناموفق",
                    username=payload.username,
                )
                await db.commit()
                invalidate_user_feed_cache(uid)
                return False
        except Exception:  # noqa: BLE001
            logger.exception("provision account=%s failed", account_id)
            return False


def start_account_provisions(account_ids: list[int]) -> list[asyncio.Task]:
    """Fire independent creates that survive the HTTP request."""
    tasks: list[asyncio.Task] = []
    for aid in account_ids:
        if not aid:
            continue
        tasks.append(
            asyncio.create_task(
                _provision_account_now(int(aid)),
                name=f"astra-acc-prov-{aid}",
            )
        )
    return tasks


async def provision_users_now(
    db: AsyncSession,
    users: list[User],
    panels: list[tuple[Panel, dict] | tuple[Panel, dict, int]],
    *,
    wait: float | None = None,
) -> dict[str, int]:
    """Enqueue every panel create; wait briefly so the first configs hit the sub."""
    targets = _normalize_panel_targets(panels)
    if not users or not targets:
        return {"ok": 0, "err": 0, "down": 0, "pending": 0}

    n = await ensure_pending_panel_accounts(db, users, targets)
    for attempt in range(8):
        try:
            await db.commit()
            break
        except OperationalError as exc:
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            if attempt >= 7:
                await db.rollback()
                n = await ensure_pending_panel_accounts(db, users, targets)
                await db.commit()
                break
            await asyncio.sleep(0.15 * (attempt + 1))
        except Exception:  # noqa: BLE001
            logger.exception("provision_users_now commit failed")
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
            n = await ensure_pending_panel_accounts(db, users, targets)
            await db.commit()
            break

    user_ids = [int(u.id) for u in users]
    rows = await db.execute(
        select(PanelAccount.id).where(
            PanelAccount.user_id.in_(user_ids),
            PanelAccount.status.in_(
                (PanelAccountStatus.pending, PanelAccountStatus.error)
            ),
        )
    )
    account_ids = [int(r[0]) for r in rows.all() if r[0]]
    tasks = start_account_provisions(account_ids)
    wait_s = (
        float(wait)
        if wait is not None
        else (2.5 if len(users) <= 2 else 3.5)
    )
    if tasks:
        await asyncio.wait(tasks, timeout=max(0.2, wait_s))
    ok = 0
    err = 0
    pending = 0
    db.expire_all()
    accs = await db.execute(
        select(PanelAccount.status).where(PanelAccount.user_id.in_(user_ids))
    )
    for (st,) in accs.all():
        if st == PanelAccountStatus.active:
            ok += 1
        elif st == PanelAccountStatus.error:
            err += 1
        elif st == PanelAccountStatus.pending:
            pending += 1
    try:
        from app.api.subscription import invalidate_user_feed_cache

        invalidate_user_feed_cache(*user_ids)
    except Exception:  # noqa: BLE001
        pass
    logger.info(
        "provision_users_now users=%s queued=%s prepared=%s ok=%s err=%s pending=%s",
        len(users),
        len(account_ids),
        n,
        ok,
        err,
        pending,
    )
    return {"ok": ok, "err": err, "down": 0, "pending": pending}


async def user_coverage_gaps(
    db: AsyncSession,
    user: User,
    *,
    include_empty_links: bool = True,
) -> list[tuple[Panel, dict, int]]:
    """Return (panel, cfg, group_id) slots the user should have but is missing live.

    Always covers missing/deleted rows. With ``include_empty_links`` also
    error/pending/active rows that have no share links (post-outage recovery).
    """
    from app.adapters.http_utils import is_share_link

    gids = [int(g.id) for g in (user.groups or []) if getattr(g, "id", None)]
    if not gids:
        ug = await db.execute(
            select(UserGroup.group_id).where(UserGroup.user_id == user.id)
        )
        gids = [int(r[0]) for r in ug.all() if r[0] is not None]
    if not gids:
        return []
    targets = await resolve_panels_for_groups(db, gids)
    if not targets:
        return []

    # Ensure accounts are loaded
    if not hasattr(user, "panel_accounts") or user.panel_accounts is None:
        acc_q = await db.execute(
            select(PanelAccount).where(PanelAccount.user_id == user.id)
        )
        user.panel_accounts = list(acc_q.scalars().all())

    by_key: dict[tuple[int, int], PanelAccount] = {}
    legacy: dict[int, PanelAccount] = {}
    for a in user.panel_accounts or []:
        gid = int(getattr(a, "group_id", 0) or 0)
        if gid:
            by_key[(int(a.panel_id), gid)] = a
        else:
            legacy[int(a.panel_id)] = a

    gaps: list[tuple[Panel, dict, int]] = []
    for panel, cfg, gid in targets:
        key = (int(panel.id), int(gid))
        acc = by_key.get(key) or legacy.get(int(panel.id))
        if acc is None or acc.status == PanelAccountStatus.deleted:
            gaps.append((panel, cfg, int(gid)))
            continue
        if not include_empty_links:
            continue
        shares = [
            str(x).strip()
            for x in (acc.links or [])
            if x and is_share_link(str(x))
        ]
        if not shares and acc.status in (
            PanelAccountStatus.error,
            PanelAccountStatus.pending,
            PanelAccountStatus.active,
        ):
            # Active-with-empty often means wipe/failure during outage
            if acc.status == PanelAccountStatus.active:
                acc.status = PanelAccountStatus.pending
                acc.error_message = None
            gaps.append((panel, cfg, int(gid)))
    return gaps


async def heal_user_coverage(
    db: AsyncSession,
    user: User,
    *,
    wait: float = 12.0,
    include_empty_links: bool = True,
) -> int:
    """Provision every missing group×panel slot for one user; freeze if limited.

    Prefer ``heal_user_coverage_pool_safe`` from background loops — this variant
    may keep ``db`` checked out while briefly waiting on provision tasks.
    """
    gaps = await user_coverage_gaps(
        db, user, include_empty_links=include_empty_links
    )
    if not gaps:
        return 0
    # Full target set (not only gaps) so ensure_pending + revive deleted works
    gids = [int(g.id) for g in (user.groups or []) if getattr(g, "id", None)]
    if not gids:
        ug = await db.execute(
            select(UserGroup.group_id).where(UserGroup.user_id == user.id)
        )
        gids = [int(r[0]) for r in ug.all() if r[0] is not None]
    panels = await resolve_panels_for_groups(db, gids)
    if not panels:
        return 0
    # Queue remotes; keep wait short so request/bg sessions are not IIT for long.
    # Actual creates run in their own pool-safe sessions.
    await provision_users_now(db, [user], panels, wait=min(float(wait), 3.0))
    try:
        await hydrate_share_links_pool_safe(int(user.id))
    except Exception:  # noqa: BLE001
        logger.debug("heal hydrate user=%s failed", user.username, exc_info=True)
    # Quota is owned by Astra — if already exhausted, cut remotes immediately
    if user.status in (
        UserStatus.limited,
        UserStatus.expired,
        UserStatus.disabled,
    ):
        try:
            await sync_modify_user(
                db,
                user,
                skip_nginx_sync=True,
                freeze_fast=True,
                remote_timeout=8,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "heal freeze user=%s failed", user.username, exc_info=True
            )
    logger.info(
        "healed coverage user=%s gaps=%s targets=%s status=%s",
        user.username,
        len(gaps),
        len(panels),
        getattr(user.status, "value", user.status),
    )
    return len(gaps)


async def heal_user_coverage_pool_safe(
    user_id: int,
    *,
    wait: float = 12.0,
    include_empty_links: bool = True,
) -> int:
    """Like heal_user_coverage but never holds a pool connection during remote I/O.

    Does not delete any rows — only ensures pending/active accounts and links.
    """
    from app.core.database import AsyncSessionLocal

    gap_n = 0
    account_ids: list[int] = []
    freeze_status: UserStatus | None = None
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User)
            .where(User.id == int(user_id))
            .options(
                selectinload(User.groups),
                selectinload(User.panel_accounts).selectinload(PanelAccount.panel),
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            return 0
        gaps = await user_coverage_gaps(
            db, user, include_empty_links=include_empty_links
        )
        if not gaps:
            return 0
        gap_n = len(gaps)
        gids = [int(g.id) for g in (user.groups or []) if getattr(g, "id", None)]
        if not gids:
            ug = await db.execute(
                select(UserGroup.group_id).where(UserGroup.user_id == user.id)
            )
            gids = [int(r[0]) for r in ug.all() if r[0] is not None]
        panels = await resolve_panels_for_groups(db, gids) if gids else []
        if not panels:
            return 0
        await ensure_pending_panel_accounts(db, [user], panels)
        await db.commit()
        rows = await db.execute(
            select(PanelAccount.id).where(
                PanelAccount.user_id == int(user_id),
                PanelAccount.status.in_(
                    (PanelAccountStatus.pending, PanelAccountStatus.error)
                ),
            )
        )
        account_ids = [int(r[0]) for r in rows.all() if r[0]]
        freeze_status = user.status
    # Session closed before waiting on remotes.

    if account_ids:
        tasks = start_account_provisions(account_ids)
        if tasks:
            await asyncio.wait(tasks, timeout=max(0.2, float(wait)))

    try:
        await hydrate_share_links_pool_safe(int(user_id))
    except Exception:  # noqa: BLE001
        logger.debug(
            "heal pool-safe hydrate user_id=%s failed", user_id, exc_info=True
        )

    if freeze_status in (
        UserStatus.limited,
        UserStatus.expired,
        UserStatus.disabled,
    ):
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(User)
                    .where(User.id == int(user_id))
                    .options(
                        selectinload(User.panel_accounts).selectinload(
                            PanelAccount.panel
                        ),
                        selectinload(User.groups),
                    )
                )
                user = result.scalar_one_or_none()
                if user is not None:
                    await sync_modify_user(
                        db,
                        user,
                        skip_nginx_sync=True,
                        freeze_fast=True,
                        remote_timeout=8,
                    )
                    await db.commit()
        except Exception:  # noqa: BLE001
            logger.warning(
                "heal pool-safe freeze user_id=%s failed", user_id, exc_info=True
            )

    logger.info(
        "healed coverage pool-safe user_id=%s gaps=%s queued=%s",
        user_id,
        gap_n,
        len(account_ids),
    )
    return gap_n


async def heal_users_missing_panel_accounts(*, limit_users: int = 20) -> int:
    """Heal users with zero OR partial panel coverage (missing group×panel slots).

    One short DB session per user — never holds a connection across HTTP.
    Never deletes accounts/users; only provisions missing slots + hydrates links.
    """
    from app.core.database import AsyncSessionLocal, background_db_sem

    n = 0
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User.id)
                .join(UserGroup, UserGroup.user_id == User.id)
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
                .distinct()
                .order_by(User.id.desc())
                .limit(max(40, int(limit_users) * 4))
            )
            candidate_ids = [int(r[0]) for r in result.all() if r[0]]

        sem = background_db_sem()
        for uid in candidate_ids:
            if n >= max(1, int(limit_users)):
                break
            async with sem:
                try:
                    needs_heal = False
                    needs_hydrate = False
                    async with AsyncSessionLocal() as db:
                        result = await db.execute(
                            select(User)
                            .where(User.id == int(uid))
                            .options(
                                selectinload(User.groups),
                                selectinload(User.panel_accounts).selectinload(
                                    PanelAccount.panel
                                ),
                            )
                        )
                        user = result.scalar_one_or_none()
                        if user is None:
                            continue
                        gaps = await user_coverage_gaps(
                            db, user, include_empty_links=True
                        )
                        if gaps:
                            needs_heal = True
                        else:
                            empty = [
                                a
                                for a in (user.panel_accounts or [])
                                if a.status
                                in (
                                    PanelAccountStatus.active,
                                    PanelAccountStatus.error,
                                    PanelAccountStatus.pending,
                                )
                                and not (a.links or [])
                            ]
                            needs_hydrate = bool(empty)
                    if needs_heal:
                        healed = await heal_user_coverage_pool_safe(
                            int(uid), wait=10.0, include_empty_links=True
                        )
                        if healed:
                            n += 1
                    elif needs_hydrate:
                        got = await asyncio.wait_for(
                            hydrate_share_links_pool_safe(int(uid)), timeout=14.0
                        )
                        if got:
                            n += 1
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "heal missing coverage user_id=%s skipped",
                        uid,
                        exc_info=True,
                    )
    except Exception:  # noqa: BLE001
        logger.exception("heal missing panel accounts failed")
    return n


async def ensure_pending_panel_accounts(
    db: AsyncSession,
    users: list[User],
    panels: list[tuple[Panel, dict] | tuple[Panel, dict, int]],
) -> int:
    """Create pending PanelAccount rows so the UI shows in-progress immediately."""
    targets = _normalize_panel_targets(panels)
    if not users or not targets:
        return 0
    user_ids = [int(u.id) for u in users]
    existing_rows = await db.execute(
        select(PanelAccount).where(PanelAccount.user_id.in_(user_ids))
    )
    have: dict[tuple[int, int, int], PanelAccount] = {}
    for a in existing_rows.scalars().all():
        have[
            (
                int(a.user_id),
                int(a.panel_id),
                int(getattr(a, "group_id", 0) or 0),
            )
        ] = a
    n = 0
    for user in users:
        uid = int(user.id)
        for panel, _cfg, group_id in targets:
            gid = int(group_id or 0)
            key = (uid, int(panel.id), gid)
            existing = have.get(key)
            if existing is not None:
                if existing.status == PanelAccountStatus.deleted:
                    existing.status = PanelAccountStatus.pending
                    existing.links = []
                    existing.links_by_group = {}
                    existing.error_message = None
                    n += 1
                continue
            acc = PanelAccount(
                user_id=uid,
                panel_id=panel.id,
                group_id=gid or None,
                status=PanelAccountStatus.pending,
            )
            db.add(acc)
            have[key] = acc
            n += 1
    if n:
        await db.flush()
    return n


def schedule_users_provision(
    user_ids: list[int],
    panel_specs: list[tuple[int, dict, int]],
) -> None:
    """Fire-and-forget remote create + hydrate so HTTP can return immediately."""
    ids = [int(x) for x in user_ids if x]
    if not ids or not panel_specs:
        return
    try:
        asyncio.create_task(
            _bg_users_provision(ids, panel_specs),
            name=f"astra-prov-{ids[0]}-n{len(ids)}",
        )
    except RuntimeError:
        pass


async def _bg_users_provision(
    user_ids: list[int],
    panel_specs: list[tuple[int, dict, int]],
) -> None:
    from app.core.database import AsyncSessionLocal
    from app.api.subscription import invalidate_user_feed_cache
    from app.services.ops_progress import finish_job_id, start_job

    n_users = len(user_ids)
    n_panels = len({int(pid) for pid, _, _ in panel_specs})
    job_id = start_job(
        "provision",
        f"اضافه کردن پنل به ساب ({n_users} کاربر · {n_panels} پنل)",
        total=n_users,
        message="در حال ساخت اکانت روی پنل‌ها و پر کردن لینک ساب…",
    )
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User)
                .where(User.id.in_(user_ids))
                .options(
                    selectinload(User.panel_accounts).selectinload(PanelAccount.panel)
                )
            )
            users = list(result.scalars().unique().all())
            pids = list({int(pid) for pid, _, _ in panel_specs})
            rows = await db.execute(
                select(Panel).where(Panel.id.in_(pids or [-1]))
            )
            by_id = {int(p.id): p for p in rows.scalars().all()}
            panels = [
                (by_id[pid], cfg, gid)
                for pid, cfg, gid in panel_specs
                if pid in by_id and bool(getattr(by_id[pid], "is_active", True))
            ]
            if users and panels:
                # One session for the batch wave is OK; cap concurrency so we
                # do not stampede remotes. Hydrate runs pool-safe AFTER close.
                await provision_users_batch(
                    db,
                    users,
                    panels,
                    create_timeout=12.0,
                    concurrency=16,
                    overall_timeout=90.0,
                )
            await db.commit()
        # Session closed — hydrate each user without holding the batch connection.
        for uid in user_ids:
            try:
                await hydrate_share_links_pool_safe(int(uid))
            except Exception:  # noqa: BLE001
                logger.warning("bg hydrate user_id=%s failed", uid, exc_info=True)
        try:
            invalidate_user_feed_cache(*user_ids)
        except Exception:  # noqa: BLE001
            pass
        await heal_failed_panel_accounts_batch(limit_users=max(10, len(user_ids)))
        finish_job_id(
            job_id,
            ok=n_users,
            message=f"اضافه به ساب تمام شد — {n_users} کاربر",
        )
    except Exception:  # noqa: BLE001
        logger.exception("bg users provision failed n=%s", len(user_ids))
        finish_job_id(job_id, failed=1, message="اضافه به ساب ناموفق بود")


async def reset_user_traffic_on_panels(
    db: AsyncSession,
    user: User,
    *,
    prefer_types: set[PanelType] | None = None,
    panel_ids: set[int] | None = None,
    remote_timeout: float = 7,
    sync_after: bool = True,
    sync_only_types: set[PanelType] | None = None,
    sync_remote_timeout: float = 7,
    concurrency: int = 20,
) -> list[dict]:
    """Reset used traffic on every remote panel in parallel, then clear local counters.

    Returns per-panel {panel_id, panel_name, ok, error}.
    Failed remotes are marked status=error so UI «خطاها» can retry.
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
    if panel_ids is not None:
        want = {int(x) for x in panel_ids}
        accounts = [a for a in accounts if int(a.panel_id) in want]

    sem = asyncio.Semaphore(max(1, int(concurrency)))

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
                account.status = PanelAccountStatus.active
                account.error_message = None
                account.last_sync_at = datetime.now(timezone.utc)
            else:
                account.status = PanelAccountStatus.error
                account.error_message = _format_remote_error(
                    panel,
                    action="بازنشانی مصرف",
                    err=err or "بازنشانی آسترا ناموفق",
                    username=uname,
                )
            return {
                "panel_id": account.panel_id,
                "panel_name": name,
                "ok": ok,
                "error": err,
            }

        adapter = get_adapter(panel)
        try:
            async with sem:
                res = await asyncio.wait_for(
                    adapter.reset_usage(uname, rid), timeout=remote_timeout
                )
        except Exception as exc:  # noqa: BLE001
            res = _fail_result(_exc_msg(exc))
        finally:
            try:
                await adapter.close()  # type: ignore[attr-defined]
            except Exception:
                pass
        account.used_traffic = 0
        account.coeff_anchor_raw = 0
        account.coeff_anchor_weighted = 0
        if res.success:
            account.status = PanelAccountStatus.active
            account.error_message = None
            account.last_sync_at = datetime.now(timezone.utc)
            return {
                "panel_id": account.panel_id,
                "panel_name": name,
                "ok": True,
                "error": None,
            }
        # Keep local zeros; mark error so UI «خطاها» + retry works
        err = res.error or "بازنشانی روی پنل ناموفق"
        account.status = PanelAccountStatus.error
        account.error_message = _format_remote_error(
            panel,
            action="بازنشانی مصرف",
            err=err,
            username=uname,
        )
        return {
            "panel_id": account.panel_id,
            "panel_name": name,
            "ok": False,
            "error": err,
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
    if panel_ids is None:
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
    else:
        all_acc_result = await db.execute(
            select(PanelAccount)
            .where(PanelAccount.user_id == user.id)
            .options(selectinload(PanelAccount.panel))
        )
        all_accounts = list(all_acc_result.scalars().all())
    await db.flush()
    ok_ids = {int(o["panel_id"]) for o in outcomes if o.get("ok")}
    fail_by_pid = {
        int(o["panel_id"]): o for o in outcomes if not o.get("ok")
    }
    if sync_after and ok_ids:
        # Re-enable / push caps only on panels that reset OK (skip failures)
        await sync_modify_user(
            db,
            user,
            only_types=sync_only_types,
            only_panel_ids=ok_ids,
            remote_timeout=sync_remote_timeout,
            skip_nginx_sync=True,
            freeze_fast=False,
        )
        # Re-zero after modify (some remotes echo stale used)
        for account in all_accounts:
            if account.status == PanelAccountStatus.deleted:
                continue
            if int(account.panel_id) in ok_ids:
                account.used_traffic = 0
                account.coeff_anchor_raw = 0
                account.coeff_anchor_weighted = 0
        # Keep reset failures visible in «خطاها» even if modify touched them
        for account in all_accounts:
            fr = fail_by_pid.get(int(account.panel_id))
            if not fr:
                continue
            uname = (account.remote_username or user.username or "").strip()
            account.status = PanelAccountStatus.error
            account.error_message = _format_remote_error(
                account.panel,
                action="بازنشانی مصرف",
                err=fr.get("error") or "بازنشانی روی پنل ناموفق",
                username=uname,
            )
    account_coeffs = await account_coefficients_for_user(db, user, all_accounts)
    _assign_user_used_traffic(
        user,
        await _weighted_total(all_accounts, account_coeffs),
        allow_shrink=True,
    )
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
    from app.services.ops_progress import finish_job_id, start_job
    from app.services.users import load_user

    job_id = start_job(
        "sync_modify",
        f"همگام‌سازی محدودیت روی پنل‌ها (کاربر #{int(user_id)})",
        total=1,
        message="در حال اعمال وضعیت/حجم روی پنل‌های ریموت…",
    )
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.id == user_id))
            row = result.scalar_one_or_none()
            if not row:
                finish_job_id(job_id, message="کاربر پیدا نشد")
                return
            user = await load_user(db, row.username)
            if not user:
                finish_job_id(job_id, message="کاربر پیدا نشد")
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
        finish_job_id(job_id, ok=1, message="همگام‌سازی پنل‌ها تمام شد")
    except Exception:  # noqa: BLE001
        logger.exception("background sync_modify failed user_id=%s", user_id)
        finish_job_id(job_id, failed=1, message="همگام‌سازی پنل‌ها ناموفق بود")


async def sync_modify_users_background(user_ids: list[int]) -> None:
    """Unfreeze/re-push remotes after batch quota flips (limited→active)."""
    from app.services.ops_progress import bump_job, finish_job_id, start_job

    ids = list(dict.fromkeys(int(x) for x in user_ids if x))[:80]
    if not ids:
        return
    # One dashboard job for the batch (avoid flooding with per-user cards)
    job_id = start_job(
        "sync_modify",
        f"همگام‌سازی محدودیت روی پنل‌ها ({len(ids)} کاربر)",
        total=len(ids),
        message="در حال اعمال وضعیت/حجم روی پنل‌های ریموت…",
    )
    ok = 0
    failed = 0
    for uid in ids:
        try:
            # Direct path without nested per-user ops cards
            from app.core.database import AsyncSessionLocal
            from app.services.users import load_user

            async with AsyncSessionLocal() as db:
                result = await db.execute(select(User).where(User.id == uid))
                row = result.scalar_one_or_none()
                if not row:
                    bump_job(job_id, ok=False)
                    failed += 1
                    continue
                user = await load_user(db, row.username)
                if not user:
                    bump_job(job_id, ok=False)
                    failed += 1
                    continue
                await sync_modify_user(
                    db,
                    user,
                    remote_timeout=20,
                    skip_nginx_sync=True,
                )
                await db.commit()
            bump_job(job_id, ok=True)
            ok += 1
        except Exception:  # noqa: BLE001
            bump_job(job_id, ok=False)
            failed += 1
            logger.debug("batch sync_modify failed user_id=%s", uid, exc_info=True)
    finish_job_id(
        job_id,
        ok=ok,
        failed=failed,
        message=(
            f"همگام‌سازی پنل‌ها تمام شد — {ok} موفق"
            + (f" · {failed} خطا" if failed else "")
        ),
    )


async def reset_remotes_background(
    user_id: int, *, exclude_types: set[PanelType] | None = None
) -> None:
    """Reset + re-push quota on foreign remotes after a fast Astra reset."""
    from app.core.database import AsyncSessionLocal
    from app.services.users import load_user

    # #region agent log
    def _dbg_reset(hid: str, msg: str, data: dict | None = None) -> None:
        import json as _json

        try:
            with open(
                "/opt/astra/data/debug-941288.ndjson", "a", encoding="utf-8"
            ) as f:
                f.write(
                    _json.dumps(
                        {
                            "sessionId": "941288",
                            "runId": "reset-fix",
                            "hypothesisId": hid,
                            "location": "orchestrator.reset_remotes_background",
                            "message": msg,
                            "data": data or {},
                            "timestamp": int(time.time() * 1000),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception:
            pass

    # #endregion

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
            _dbg_reset(
                "H-F",
                "reset_remotes_start",
                {
                    "user_id": user_id,
                    "username": user.username,
                    "target_n": len(targets),
                    "pg_n": sum(
                        1
                        for a in targets
                        if a.panel and a.panel.type == PanelType.pasarguard
                    ),
                },
            )

            async def _one(account: PanelAccount) -> dict:
                panel = account.panel
                if not panel:
                    return {
                        "panel_id": account.panel_id,
                        "ok": False,
                        "error": "no panel",
                    }
                adapter = get_adapter(panel)
                uname = (account.remote_username or user.username or "").strip()
                rid = account.remote_id or account.remote_username or uname
                ok = False
                err: str | None = None
                try:
                    res = await asyncio.wait_for(
                        adapter.reset_usage(uname, rid), timeout=18
                    )
                    ok = bool(res.success)
                    err = None if ok else (res.error or "reset failed")
                    if not ok:
                        # One retry after token refresh / short pause
                        try:
                            await adapter.close()  # type: ignore[attr-defined]
                        except Exception:
                            pass
                        await asyncio.sleep(0.35)
                        adapter = get_adapter(panel)
                        res2 = await asyncio.wait_for(
                            adapter.reset_usage(uname, rid), timeout=18
                        )
                        ok = bool(res2.success)
                        err = None if ok else (res2.error or err or "reset failed")
                except Exception as exc:  # noqa: BLE001
                    ok = False
                    err = _exc_msg(exc)
                finally:
                    try:
                        await adapter.close()  # type: ignore[attr-defined]
                    except Exception:
                        pass
                account.used_traffic = 0
                account.coeff_anchor_raw = 0
                account.coeff_anchor_weighted = 0
                # Clear freeze mark so sync_modify re-enables disabled remotes
                if (account.error_message or "") == "ASTRA_QUOTA_FROZEN":
                    account.error_message = None
                if not ok and err:
                    low = err.lower()
                    if "admin is limited" in low or "write actions blocked" in low:
                        meta = dict(panel.meta or {})
                        meta["pg_admin_write_blocked"] = True
                        meta["pg_admin_write_blocked_at"] = datetime.now(
                            timezone.utc
                        ).isoformat()
                        meta["pg_admin_write_blocked_err"] = err[:200]
                        panel.meta = meta
                        account.status = PanelAccountStatus.error
                        account.error_message = (
                            "ادمین پنل پاسارگاد محدود شده — بازنشانی/قطع روی این پنل ممکن نیست"
                        )
                return {
                    "panel_id": account.panel_id,
                    "panel_name": panel.name,
                    "panel_type": getattr(panel.type, "value", str(panel.type)),
                    "ok": ok,
                    "error": err,
                }

            outcomes: list[dict] = []
            if targets:
                outcomes = list(await asyncio.gather(*[_one(a) for a in targets]))
            fail_n = sum(1 for o in outcomes if not o.get("ok"))
            _dbg_reset(
                "H-F",
                "reset_remotes_done",
                {
                    "user_id": user_id,
                    "ok_n": len(outcomes) - fail_n,
                    "fail_n": fail_n,
                    "fails": [o for o in outcomes if not o.get("ok")][:12],
                },
            )
            await sync_modify_user(
                db,
                user,
                exclude_types=exclude_types,
                remote_timeout=20,
                skip_nginx_sync=True,
            )
            # Zero again after re-enable push (some remotes report stale used)
            for account in accounts:
                if account.status == PanelAccountStatus.deleted:
                    continue
                account.used_traffic = 0
                account.coeff_anchor_raw = 0
                account.coeff_anchor_weighted = 0
            user.used_traffic = 0
            if user.status == UserStatus.limited:
                user.status = UserStatus.active
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("background reset remotes failed user_id=%s", user_id)
        _dbg_reset("H-F", "reset_remotes_exc", {"user_id": user_id})


async def sync_modify_user(
    db: AsyncSession,
    user: User,
    *,
    only_types: set[PanelType] | None = None,
    exclude_types: set[PanelType] | None = None,
    only_panel_ids: set[int] | None = None,
    remote_timeout: float = 25,
    skip_nginx_sync: bool = False,
    freeze_fast: bool = False,
) -> None:
    """Push status + shared remaining quota to every remote panel account.

    When Astra is limited/expired/disabled, every panel type (Marzban / PasarGuard /
    Sanaei / Rebecca / Astra) must receive the freeze. freeze_fast=True cuts all
    remotes in parallel with a ~1s budget (next watch cycle retries misses).
    """
    result = await db.execute(
        select(PanelAccount)
        .where(PanelAccount.user_id == user.id)
        .options(selectinload(PanelAccount.panel))
    )
    accounts = list(result.scalars().all())
    if only_panel_ids is not None:
        want = {int(x) for x in only_panel_ids}
        accounts = [a for a in accounts if int(a.panel_id) in want]

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
    _assign_user_used_traffic(user, weighted)

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
        # Quota cut: PasarGuard must hit /disabled first; skip heavy PUT on freeze
        if blocked and panel.type == PanelType.pasarguard:
            payload.config = {
                **(payload.config or {}),
                "_freeze_only": True,
            }
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
        # Fast freeze: one short shot so every panel is cut in ~1s.
        # Misses retry on the next 3s watch cycle (account marked error).
        if freeze_fast:
            attempts = 1
        elif blocked:
            attempts = 3
        else:
            attempts = 1
        last = _fail_result("modify failed")
        for i in range(attempts):
            adapter = get_adapter(panel)
            try:
                async with sem:
                    last = await asyncio.wait_for(
                        adapter.modify_user(payload),
                        timeout=timeout,
                    )
                if last.success:
                    return last
                # Missing / reserved name → create with collision aliases.
                # Skip on freeze_fast: a missing remote is already offline.
                if (
                    not freeze_fast
                    and (
                        _is_not_found_err(last.error)
                        or _is_username_taken_err(last.error)
                    )
                ):
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
    from app.services.panel_usage_bulk import QUOTA_FROZEN_MARK

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
            # Mark confirmed remote freeze so watch does not re-queue forever;
            # clear when user is active again so a later limit re-freezes.
            account.error_message = QUOTA_FROZEN_MARK if blocked else None
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
    _assign_user_used_traffic(
        user, await _weighted_total(accounts, account_coeffs)
    )
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
    fast: bool = False,
) -> list[dict]:
    """Cut remotes for these accounts and mark them deleted locally.

    Share links are wiped first so the sub drops configs immediately.
    ``fast=True``: ~1s parallel delete, then disable fallback; local row is
    always marked deleted so quota stays (used_traffic kept) and heal cannot
    recreate the tunnel. used_traffic is never reset.
    """
    if not accounts:
        return []

    # Sub feed first — never wait on remote HTTP to hide configs
    if clear_links_always:
        for a in accounts:
            a.links = []
            a.links_by_group = {}
        await db.flush()

    sem = _remote_sem()
    t0 = time.monotonic()
    timeout = 1.15 if fast else 20.0
    attempts = 1 if fast else 3

    async def _disable(account: PanelAccount, panel: Panel, uname: str) -> bool:
        adapter = get_adapter(panel)
        try:
            payload = UserPayload(
                username=uname,
                status="disabled",
                data_limit=1,
                expire=1,
                uuid=account.uuid,
                config={
                    "_remote_id": (account.remote_id or "").strip(),
                    "_astra_skip_nginx": True,
                },
            )
            async with sem:
                res = await asyncio.wait_for(
                    adapter.modify_user(payload), timeout=timeout
                )
            return bool(res and res.success)
        except Exception:  # noqa: BLE001
            return False
        finally:
            try:
                await adapter.close()  # type: ignore[attr-defined]
            except Exception:
                pass

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

        last_err = "حذف از پنل ناموفق بود"
        for attempt in range(attempts):
            adapter = get_adapter(panel)
            try:
                async with sem:
                    res = await asyncio.wait_for(
                        adapter.delete_user(
                            uname, account.remote_id or account.remote_username
                        ),
                        timeout=timeout,
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
            if attempt + 1 < attempts:
                await asyncio.sleep(0.2)

        if await _disable(account, panel, uname):
            return {**base, "ok": True, "error": None}
        return {**base, "ok": False, "error": last_err}

    outcomes = list(await asyncio.gather(*[_remote(a) for a in accounts]))

    by_id = {a.id: a for a in accounts}
    for row in outcomes:
        account = by_id.get(row.get("account_id"))
        if account is None:
            continue
        # Always retire locally — used_traffic kept for Astra quota
        account.links = []
        account.links_by_group = {}
        account.status = PanelAccountStatus.deleted
        account.error_message = None if row.get("ok") else (
            row.get("error") or "قطع ریموت ناموفق"
        )

    await db.flush()
    # Bank deleted meters into user.used_traffic immediately (monotonic).
    # Without this, a later recount that misses deleted rows refunds volume.
    try:
        await recount_user_used_traffic(db, user)
    except Exception:  # noqa: BLE001
        logger.warning(
            "post-delete recount failed user=%s", user.username, exc_info=True
        )
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
    Covers Marzban / Sanaei / PasarGuard / Rebecca / Astra node — works even when
    the Astra panel row is already inactive.
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

    # Drop share links first so subs update even if remote delete is slow
    touched_uids: list[int] = []
    seen_u: set[int] = set()
    for a in accounts:
        a.links = []
        a.links_by_group = {}
        uid = int(a.user_id)
        if uid not in seen_u:
            seen_u.add(uid)
            touched_uids.append(uid)
    await db.flush()
    try:
        from app.api.subscription import invalidate_user_feed_cache

        if touched_uids:
            invalidate_user_feed_cache(*touched_uids)
    except Exception:  # noqa: BLE001
        pass

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
            a.error_message = None
        # Bank usage onto the user BEFORE panel CASCADE wipes these rows
        try:
            await recount_user_used_traffic(db, user)
        except Exception:  # noqa: BLE001
            logger.warning(
                "deprovision-panel recount failed user_id=%s", uid, exc_info=True
            )

    await db.flush()
    try:
        from app.api.subscription import invalidate_user_feed_cache

        if touched_uids:
            invalidate_user_feed_cache(*touched_uids)
    except Exception:  # noqa: BLE001
        pass
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
    *,
    fast: bool = True,
) -> list[dict]:
    """
    Delete remote accounts that belong to the given Astra groups, then mark
    those PanelAccount rows deleted.

    ``used_traffic`` on retired rows is never cleared — quota stays banked.

    Used when an admin removes groups from a user / category. Also catches
    legacy rows with ``group_id=NULL`` whose panel belongs only to the
    removed groups (not claimed by the user's remaining membership).
    """
    from app.models.group import GroupPanel

    want = {int(g) for g in group_ids if g is not None}
    if not want:
        return []

    result = await db.execute(
        select(PanelAccount)
        .where(PanelAccount.user_id == user.id)
        .options(selectinload(PanelAccount.panel))
    )
    all_acc = list(result.scalars().all())
    accounts = [
        a
        for a in all_acc
        if a.status != PanelAccountStatus.deleted
        and getattr(a, "group_id", None) is not None
        and int(a.group_id) in want
    ]
    seen = {int(a.id) for a in accounts}

    # Legacy / orphan: null group_id on a panel that only the removed groups claim
    ug = await db.execute(
        select(UserGroup.group_id).where(UserGroup.user_id == int(user.id))
    )
    remaining = {
        int(r[0])
        for r in ug.all()
        if r[0] is not None and int(r[0]) not in want
    }
    removed_panel_ids: set[int] = set()
    keep_panel_ids: set[int] = set()
    gp = await db.execute(
        select(GroupPanel.group_id, GroupPanel.panel_id).where(
            GroupPanel.group_id.in_(sorted(want | remaining) or [-1])
        )
    )
    for gid, pid in gp.all():
        if gid is None or pid is None:
            continue
        if int(gid) in want:
            removed_panel_ids.add(int(pid))
        if int(gid) in remaining:
            keep_panel_ids.add(int(pid))
    orphan_panels = removed_panel_ids - keep_panel_ids
    if orphan_panels:
        for a in all_acc:
            if int(a.id) in seen:
                continue
            if a.status == PanelAccountStatus.deleted:
                continue
            if getattr(a, "group_id", None) is not None:
                continue
            if int(a.panel_id) in orphan_panels:
                accounts.append(a)
                seen.add(int(a.id))

    return await _delete_panel_accounts_remote(
        db, user, accounts, fast=bool(fast)
    )


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
    return await _delete_panel_accounts_remote(db, user, accounts, fast=True)


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
        "panels_down_detail": [],
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
    probe_err: dict[int, str] = {}
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
                probe_err[pid] = "Timeout waiting for panel response"
                continue
            try:
                state, err = task.result()
                probe_state[pid] = (
                    state if state in ("ok", "soft", "down") else "soft"
                )
                probe_err[pid] = (err or "").strip()
            except Exception as exc:  # noqa: BLE001
                probe_state[pid] = "soft"
                probe_err[pid] = _exc_msg(exc) or "probe failed"

    panels_ok_names: list[str] = []
    panels_down_names: list[str] = []
    panels_down_detail: list[dict[str, str]] = []
    ok_accounts: list[PanelAccount] = []
    soft_accounts: list[PanelAccount] = []
    down_accounts: list[PanelAccount] = []
    skipped = 0
    down_seen: set[int] = set()
    for a in live:
        pid = int(a.panel_id)
        name = (a.panel.name if a.panel else None) or f"#{pid}"
        state = probe_state.get(pid, "soft")
        if state == "down":
            down_accounts.append(a)
            skipped += 1
            if pid not in down_seen:
                down_seen.add(pid)
                panels_down_names.append(name)
                panels_down_detail.append(
                    {
                        "name": name,
                        "reason": _short_panel_down_reason(probe_err.get(pid)),
                        "detail": (probe_err.get(pid) or "")[:160],
                    }
                )
            continue
        if state == "ok":
            ok_accounts.append(a)
            if name not in panels_ok_names:
                panels_ok_names.append(name)
        else:
            soft_accounts.append(a)
            if name not in panels_ok_names:
                panels_ok_names.append(name)

    # Stamp clear reasons on broken panels (auth / hard-down) — no wasted retries.
    # Flush only (no mid-transaction commit) so ORM rows stay usable until the
    # final commit at the end of this retry pass.
    stamped = 0
    for a in down_accounts:
        panel = a.panel
        err = probe_err.get(int(a.panel_id), "") or "پنل در دسترس نیست"
        a.status = PanelAccountStatus.error
        a.error_message = _format_remote_error(
            panel,
            action="بررسی اتصال پنل",
            err=err,
            username=(a.remote_username or None),
        )
        stamped += 1
    if stamped:
        try:
            await db.flush()
        except Exception:  # noqa: BLE001
            logger.debug("retry-all stamp down errors flush failed", exc_info=True)
        logger.info(
            "retry-all stamped down-panel errors count=%s panels=%s",
            stamped,
            ",".join(panels_down_names) or "-",
        )

    def _fair_round_robin(rows: list[PanelAccount]) -> list[PanelAccount]:
        """Don't let one fat panel starve recoveries on other healthy panels."""
        from collections import defaultdict, deque

        buckets: dict[int, deque[PanelAccount]] = defaultdict(deque)
        for row in rows:
            buckets[int(row.panel_id)].append(row)
        out: list[PanelAccount] = []
        while buckets:
            for pid in list(buckets.keys()):
                q = buckets[pid]
                if q:
                    out.append(q.popleft())
                if not q:
                    del buckets[pid]
        return out

    # Sticky «Timeout / قطع» on panels that just probed OK/soft are false alarms —
    # requeue as pending so heal actually retries instead of leaving scary text.
    requeued = 0
    for a in ok_accounts + soft_accounts:
        if a.status != PanelAccountStatus.error:
            continue
        msg = a.error_message or ""
        if _is_timeout_err(msg) or (
            _is_unreachable_err(msg) and not _is_auth_err(msg)
        ):
            a.status = PanelAccountStatus.pending
            a.error_message = None
            requeued += 1
    if requeued:
        try:
            await db.flush()
        except Exception:  # noqa: BLE001
            pass
        logger.info(
            "retry-all requeued false timeout/unreachable errors count=%s",
            requeued,
        )

    # Healthy first, then soft — fair across panels; limit AFTER probe so a
    # broken fat panel (wrong password) cannot eat the whole batch.
    reachable = _fair_round_robin(ok_accounts) + _fair_round_robin(soft_accounts)
    if limit_accounts is not None and limit_accounts > 0:
        reachable = reachable[: int(limit_accounts)]

    stats["skipped_unreachable"] = skipped
    stats["panels_ok"] = panels_ok_names
    stats["panels_down"] = panels_down_names
    stats["panels_down_detail"] = panels_down_detail
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


async def clear_panel_account_share_links(
    db: AsyncSession, panel_id: int
) -> list[int]:
    """Wipe share links for every non-deleted account on this panel.

    Subscription feeds stop emitting those configs immediately (inactive panel
    is already filtered, but empty links keep hydrate/order paths clean).
    Returns touched user ids for feed-cache invalidation.
    """
    pid = int(panel_id)
    result = await db.execute(
        select(PanelAccount).where(
            PanelAccount.panel_id == pid,
            PanelAccount.status != PanelAccountStatus.deleted,
        )
    )
    rows = list(result.scalars().all())
    touched: list[int] = []
    seen: set[int] = set()
    for a in rows:
        a.links = []
        a.links_by_group = {}
        uid = int(a.user_id)
        if uid not in seen:
            seen.add(uid)
            touched.append(uid)
    if rows:
        await db.flush()
        logger.info(
            "clear panel share-links panel=%s accounts=%s users=%s",
            pid,
            len(rows),
            len(touched),
        )
    return touched


async def freeze_all_accounts_on_panel(panel_id: int) -> dict:
    """Disable every remote user on this panel (Marzban/Sanaei/Pasar/Rebecca/Astra).

    Used after an Astra panel is turned OFF. Local PanelAccount rows stay
    (status unchanged, links already cleared) so heal can re-enable later.
    Runs in its own DB session — safe as a background task.
    """
    from app.core.database import AsyncSessionLocal

    pid = int(panel_id)
    stats = {"accounts": 0, "ok": 0, "fail": 0, "skipped": 0}
    t0 = time.monotonic()
    try:
        async with AsyncSessionLocal() as db:
            panel = await db.get(Panel, pid)
            if panel is None:
                return stats
            # Panel may already be inactive — that is expected.

            gp = await db.execute(
                select(GroupPanel).where(GroupPanel.panel_id == pid)
            )
            claim_cfg: dict[int, dict] = {}
            any_cfg: dict = {}
            for link in gp.scalars().all():
                cfg = dict(link.config or {})
                claim_cfg[int(link.group_id)] = cfg
                if cfg and not any_cfg:
                    any_cfg = cfg

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
            stats["accounts"] = len(accounts)
            if not accounts:
                return stats

            # Ensure links stay empty even if caller skipped clear
            for a in accounts:
                a.links = []
                a.links_by_group = {}
            await db.commit()

            sem = _remote_sem()

            async def _freeze_remote(account: PanelAccount) -> str:
                """Return ok | fail | skipped."""
                p = account.panel
                user = account.user
                if p is None or user is None:
                    return "fail"
                uname = (account.remote_username or user.username or "").strip()
                if not uname:
                    return "skipped"  # never provisioned

                gid = int(getattr(account, "group_id", 0) or 0)
                cfg = dict(claim_cfg.get(gid) or any_cfg or {})
                rid = (account.remote_id or "").strip()
                if rid:
                    cfg["_remote_id"] = rid

                payload = build_user_payload(
                    user,
                    uuid=account.uuid,
                    panel_config=cfg,
                    used_raw_on_panel=int(account.used_traffic or 0),
                    remote_username=uname,
                )
                # Force remote freeze regardless of Astra user status
                payload.status = "disabled"
                used_r = max(0, int(account.used_traffic or 0))
                if int(user.data_limit or 0) > 0:
                    payload.data_limit = max(1, used_r) if used_r > 0 else 1

                last_err: str | None = None
                for attempt in range(3):
                    adapter = get_adapter(p)
                    try:
                        async with sem:
                            res = await asyncio.wait_for(
                                adapter.modify_user(payload),
                                timeout=18.0,
                            )
                        if res.success or _is_not_found_err(res.error):
                            return "ok"
                        last_err = res.error
                    except Exception as exc:  # noqa: BLE001
                        last_err = _exc_msg(exc)
                    finally:
                        try:
                            await adapter.close()  # type: ignore[attr-defined]
                        except Exception:
                            pass
                    if attempt < 2:
                        await asyncio.sleep(0.3 + attempt * 0.2)

                logger.warning(
                    "freeze-panel panel=%s account=%s user=%s fail=%s",
                    pid,
                    account.id,
                    uname,
                    last_err,
                )
                return "fail"

            # Astra node clients: upsert on shared session (avoids SQLite lock)
            astra_accounts: list[PanelAccount] = []
            remote_jobs: list[PanelAccount] = []
            for a in accounts:
                p = a.panel
                if p is not None and p.type == PanelType.astra:
                    astra_accounts.append(a)
                else:
                    remote_jobs.append(a)

            if astra_accounts:
                from app.services import astra_node as astra_node_svc

                panel_row = await db.get(Panel, pid)
                if panel_row is not None:
                    for account in astra_accounts:
                        user = account.user
                        uname = (
                            (
                                account.remote_username
                                or (user.username if user else "")
                                or ""
                            ).strip()
                        )
                        if not uname or user is None:
                            stats["skipped"] += 1
                            continue
                        try:
                            await astra_node_svc.upsert_client(
                                db,
                                panel_row,
                                email=uname,
                                uuid=account.uuid,
                                data_limit=max(1, int(account.used_traffic or 0)) or 1,
                                expire=0,
                                enable=False,
                                limit_ip=0,
                                rebuild=False,
                                sync_nginx=False,
                            )
                            stats["ok"] += 1
                        except Exception:  # noqa: BLE001
                            stats["fail"] += 1
                            logger.exception(
                                "freeze-panel astra account=%s failed", account.id
                            )
                    try:
                        await astra_node_svc.rebuild_xray_config(db, panel_row)
                    except Exception:  # noqa: BLE001
                        logger.exception("freeze-panel astra rebuild panel=%s", pid)
                    await db.commit()

            if remote_jobs:
                outcomes = await asyncio.gather(
                    *[_freeze_remote(a) for a in remote_jobs],
                    return_exceptions=True,
                )
                for o in outcomes:
                    if o == "ok":
                        stats["ok"] += 1
                    elif o == "skipped":
                        stats["skipped"] += 1
                    else:
                        stats["fail"] += 1
                        if not isinstance(o, str):
                            logger.warning("freeze-panel gather err: %s", o)

            try:
                from app.api.subscription import invalidate_user_feed_cache

                uids = sorted({int(a.user_id) for a in accounts})
                if uids:
                    invalidate_user_feed_cache(*uids)
            except Exception:  # noqa: BLE001
                pass

            logger.info(
                "freeze-panel panel=%s accounts=%s ok=%s fail=%s skipped=%s dt=%.2fs",
                pid,
                stats["accounts"],
                stats["ok"],
                stats["fail"],
                stats["skipped"],
                time.monotonic() - t0,
            )
    except Exception:  # noqa: BLE001
        stats["fail"] += 1
        logger.exception("freeze-panel panel=%s fatal", pid)
    return stats


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
                overall_timeout=160.0,
                per_remote_timeout=18.0,
                probe_timeout=10.0,
                concurrency=18,
                # Prefer finishing a fair batch on healthy panels
                limit_accounts=max(120, int(limit_users) * 8),
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
    # Shared with link_remark — never mid-cut coefficients like x1.2 → x1
    from app.services.link_remark import _clip_config_tag

    return _clip_config_tag(raw)


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
        select(
            Group.id,
            Group.config_tag,
            Group.name,
            Group.order_kind,
            Group.usage_cost,
        )
        .where(Group.id.in_(gids), Group.is_disabled.is_(False))
        .order_by(Group.id.asc())
    )
    from app.services.ping_category_order import normalize_order_kind as _nok_claim

    meta_by_gid = {
        int(gid): (
            _normalize_config_tag(tag),
            str(name or ""),
            _nok_claim(str(order_kind or "other")),
            usage_cost,
        )
        for gid, tag, name, order_kind, usage_cost in g_rows.all()
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
        tag, name, order_kind, usage_cost = meta_by_gid.get(
            int(gid), ("", "", "other", None)
        )
        claims.append(
            {
                "group_id": int(gid),
                "panel_id": pid_i,
                "tag": tag,
                "name": name,
                "order_kind": order_kind,
                "usage_cost": usage_cost,
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
        from app.services.ping_category_order import normalize_order_kind as _nok

        order_kind = _nok(str(getattr(group, "order_kind", None) or "other"))
        usage_cost = getattr(group, "usage_cost", None)
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
                    "order_kind": order_kind,
                    "usage_cost": usage_cost,
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
    rank_operator: str | None = None,
) -> list[str]:
    rows = await collect_links_detailed(
        user,
        include_notices=include_notices,
        include_hidden=include_hidden,
        db=db,
        live_rebuild=live_rebuild,
        rank_operator=rank_operator,
    )
    return [row["link"] for row in rows if row.get("link")]


async def collect_links_detailed(
    user: User,
    *,
    include_notices: bool = True,
    include_hidden: bool = False,
    db: AsyncSession | None = None,
    live_rebuild: bool = True,
    rank_operator: str | None = None,
) -> list[dict]:
    """All unique share links across panels.

    Order (every sub — client feed + HTML):
      1) status row (✨ user - remain - days ✨) — always, live values
      2) promo rows — up to 2 when enabled in settings
      3) group notices (کانفیگ اضافه) — optional, unchanged text
      4) real configs — up to 20 مستقیم then تانل / گیمینگ / leftover

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
        quota_block_notices,
    )
    from app.services.site_settings import (
        get_promo_config_texts_for_user,
        get_status_line_template,
        get_status_panel_label,
    )

    used = int(user.used_traffic or 0)
    limit = int(user.data_limit or 0)
    # Status line uses login username (stable), not display_name
    status_name = (getattr(user, "username", None) or "user").strip() or "user"
    user_status_s = (
        user.status.value
        if hasattr(getattr(user, "status", None), "value")
        else str(getattr(user, "status", "") or "")
    )
    expire_ts = getattr(user, "expire", None)
    if getattr(user, "status", None) == UserStatus.on_hold:
        hold_dur = _on_hold_duration_seconds(user)
        if hold_dur > 0:
            expire_ts = int(time.time()) + hold_dur
    groups = list(getattr(user, "groups", None) or user.__dict__.get("groups") or [])

    # Prefer fresh order_kind/usage_cost from DB when available so admin
    # «نوع ترتیب ساب» changes apply on the next /sub hit (not a stale graph).
    loaded_claims = _claims_from_loaded_groups(user)
    if db is not None:
        try:
            claims = await resolve_group_panel_claims(db, user)
        except Exception:  # noqa: BLE001
            claims = loaded_claims
        if not claims:
            claims = loaded_claims
    else:
        claims = loaded_claims

    from app.services.ping_category_order import normalize_order_kind

    tag_by_gid = {str(c["group_id"]): _normalize_config_tag(c.get("tag")) for c in claims}
    kind_by_gid: dict[str, str] = {}
    cost_by_gid: dict[str, object] = {}
    tag_by_panel: dict[int, str] = {}
    kind_by_panel: dict[int, str] = {}
    cost_by_panel: dict[int, object] = {}
    for c in claims:
        gid_s = str(c["group_id"])
        pid_c = int(c.get("panel_id") or 0)
        t = _normalize_config_tag(c.get("tag"))
        ok = normalize_order_kind(str(c.get("order_kind") or "other"))
        uc = c.get("usage_cost")
        if gid_s not in kind_by_gid:
            kind_by_gid[gid_s] = ok
            cost_by_gid[gid_s] = uc
        if pid_c and t and pid_c not in tag_by_panel:
            tag_by_panel[pid_c] = t
        # Prefer a real bucket over «other» when several groups share a panel
        if pid_c:
            prev_k = kind_by_panel.get(pid_c)
            if prev_k is None or (
                prev_k == "other" and ok != "other"
            ):
                kind_by_panel[pid_c] = ok
                cost_by_panel[pid_c] = uc

    def _tag_for(gid: object, pid: int) -> str:
        if gid is not None:
            t = tag_by_gid.get(str(int(gid)), "") or ""
            if t:
                return t
        return tag_by_panel.get(int(pid), "") or ""

    def _kind_cost_for(gid: object, pid: int) -> tuple[str, object]:
        if gid is not None:
            gs = str(int(gid))
            if gs in kind_by_gid:
                return kind_by_gid[gs], cost_by_gid.get(gs)
        return (
            kind_by_panel.get(int(pid), "other"),
            cost_by_panel.get(int(pid)),
        )

    head: list[dict] = [
        build_status_row(
            username=status_name,
            used=used,
            limit=limit,
            expire=expire_ts,
            template=get_status_line_template(),
            panel_label=get_status_panel_label(),
            user_status=user_status_s,
        )
    ]
    # Always show on feed+HTML when volume/time finished (apps can still update)
    head.extend(quota_block_notices(user_status_s))
    for promo_text in get_promo_config_texts_for_user(
        include_first=bool(getattr(user, "include_promo_first", True))
    ):
        promo = build_promo_row(promo_text)
        if promo:
            head.append(promo)

    notices = notice_rows_from_groups(groups) if include_notices else []
    rows: list[dict] = []
    seen: set[str] = set()
    by_link: dict[str, dict] = {}

    def _emit(text: str, **fields: object) -> None:
        # Never let loopback / placeholder share links eat the 99-cap slots
        try:
            from app.services.config_locations import extract_link_endpoint

            ep = extract_link_endpoint(text)
            if ep:
                host_l = str(ep[0] or "").strip().lower().strip("[]")
                if host_l in ("127.0.0.1", "0.0.0.0", "::1", "localhost"):
                    return
        except Exception:  # noqa: BLE001
            pass
        gid_f = fields.get("group_id")
        pid_f = int(fields.get("panel_id") or 0)
        if "order_kind" not in fields or "usage_cost" not in fields:
            kind, cost = _kind_cost_for(gid_f, pid_f)
            fields.setdefault("order_kind", kind)
            fields.setdefault("usage_cost", cost)
        prev = by_link.get(text)
        if prev is not None:
            new_tag = str(fields.get("config_tag") or "").strip()
            if new_tag and not str(prev.get("config_tag") or "").strip():
                prev["config_tag"] = new_tag
                if fields.get("group_id") is not None:
                    prev["group_id"] = fields.get("group_id")
            # Prefer a real order_kind over leftover "other" on duplicate links
            new_kind = str(fields.get("order_kind") or "other")
            old_kind = str(prev.get("order_kind") or "other")
            if new_kind != "other" and old_kind == "other":
                prev["order_kind"] = new_kind
                prev["usage_cost"] = fields.get("usage_cost")
                if fields.get("group_id") is not None:
                    prev["group_id"] = fields.get("group_id")
            return
        seen.add(text)
        row = {"link": text, **fields}
        by_link[text] = row
        rows.append(row)

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

    blocked_user = user.status in (
        UserStatus.limited,
        UserStatus.expired,
        UserStatus.disabled,
    )

    def _panel_write_blocked(panel: object | None) -> bool:
        """PasarGuard admin limited → remotes cannot be disabled; drop from exhausted sub."""
        if panel is None:
            return False
        meta = getattr(panel, "meta", None) or {}
        if not isinstance(meta, dict):
            return False
        return bool(meta.get("pg_admin_write_blocked"))

    # Probe / AstraPing (include_hidden): every live account with links — including
    # temporarily inactive panels — so the app can clean the full inventory.
    # Normal clients: only active panels still claimed by the user's groups.
    active_accounts = [
        a
        for a in (user.panel_accounts or [])
        if a.status
        in (
            PanelAccountStatus.active,
            PanelAccountStatus.error,
            PanelAccountStatus.pending,
        )
        and (
            include_hidden
            or (
                _panel_is_active(
                    a.__dict__.get("panel") or getattr(a, "panel", None)
                )
                and (not claim_keys or _account_in_membership(a))
                # Exhausted users: omit write-blocked PG panels so refresh drops dead configs
                and not (
                    blocked_user
                    and _panel_write_blocked(
                        a.__dict__.get("panel") or getattr(a, "panel", None)
                    )
                )
            )
        )
    ]

    # Astra live rewrite is local SQL — skip on the tunneled /sub GET so nginx
    # never waits. Outbound add/toggle already rewrites PanelAccount.links.
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
            uname = (getattr(user, "username", None) or "").strip()
            fresh = list(astra_links_cache[pid].get(email) or [])
            if not fresh and uname:
                fresh = list(astra_links_cache[pid].get(uname) or [])
            if not fresh:
                continue
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
            tag = _tag_for(acct_gid, pid)
            for link in account.links or []:
                text = (link or "").strip()
                if not text or not is_share_link(text):
                    continue
                if not include_hidden and _config_hidden(text):
                    continue
                _emit(
                    text,
                    panel=label,
                    panel_id=pid,
                    config_tag=tag,
                    group_id=int(acct_gid),
                )
            continue

        panel_claims = [c for c in claims if int(c.get("panel_id") or 0) == pid]
        by_group = partitioned_map.get(account.id) or {}

        if not panel_claims:
            for link in account.links or []:
                text = (link or "").strip()
                if not text or not is_share_link(text):
                    continue
                if not include_hidden and _config_hidden(text):
                    continue
                _emit(
                    text,
                    panel=label,
                    panel_id=pid,
                    config_tag=_tag_for(acct_gid, pid),
                    group_id=int(acct_gid) if acct_gid is not None else None,
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
            gid_i = int(gid) if str(gid).isdigit() else 0
            tag = _tag_for(gid_i, pid)
            for link in by_group.get(gid) or []:
                text = (link or "").strip()
                if not text or not is_share_link(text):
                    continue
                if not include_hidden and _config_hidden(text):
                    continue
                _emit(
                    text,
                    panel=label,
                    panel_id=pid,
                    config_tag=tag,
                    group_id=gid_i,
                )

    if astra_mutated and live_rebuild and db is not None:
        try:
            await db.flush()
        except Exception:  # noqa: BLE001
            pass

    # Dedupe by endpoint fingerprint, then arrange THIS user's inventory by
    # group.order_kind (20 direct → tunnel budget → 9 gaming). Ping/app scores
    # only reorder inside a slot — never move a config across buckets.
    from app.services.config_locations import (
        extract_link_client_id,
        extract_link_endpoint,
        extract_link_match_host,
        extract_link_path,
        extract_link_remark_key,
        try_normalize_endpoint,
    )
    from app.services.config_orders import (
        has_operator_order_map,
        link_order_index,
        to_ping_operator,
    )
    from app.services.ping_category_order import (
        SUB_MAX_CONFIGS,
        arrange_subscription_rows,
        normalize_order_kind,
    )

    # Probe / AstraPing (include_hidden): keep every distinct share URI so the
    # app can test/clean the full inventory. Normal clients still fingerprint-
    # collapse identical endpoints so the curated 99 feed stays clean.
    if include_hidden:
        for row in rows:
            row["order_kind"] = normalize_order_kind(
                str(row.get("order_kind") or "other")
            )
    else:
        by_fp: dict[tuple, dict] = {}
        no_fp: list[dict] = []
        for row in rows:
            row["order_kind"] = normalize_order_kind(
                str(row.get("order_kind") or "other")
            )
            link = str(row.get("link") or "")
            ep = extract_link_endpoint(link)
            key: tuple | None = None
            if ep:
                mh = extract_link_match_host(link) or ""
                norm = try_normalize_endpoint(ep[0], ep[1], mh)
                if norm:
                    path = extract_link_path(link)
                    remark = extract_link_remark_key(link)
                    # Never silently collapse distinct clients that share host:port:sni
                    # with empty path/remark (common on Guard/Marzban multi-inbound).
                    if not path and not remark:
                        remark = extract_link_client_id(link)
                    key = (*norm, path, remark)
            if key is None:
                no_fp.append(row)
                continue
            prev = by_fp.get(key)
            if prev is None:
                by_fp[key] = row
                continue
            new_tag = str(row.get("config_tag") or "").strip()
            old_tag = str(prev.get("config_tag") or "").strip()
            if new_tag and not old_tag:
                prev["config_tag"] = new_tag
                if row.get("group_id") is not None:
                    prev["group_id"] = row.get("group_id")
            new_kind = normalize_order_kind(str(row.get("order_kind") or "other"))
            old_kind = normalize_order_kind(str(prev.get("order_kind") or "other"))
            if new_kind != "other" and old_kind == "other":
                prev["order_kind"] = new_kind
                prev["usage_cost"] = row.get("usage_cost")
                if row.get("group_id") is not None:
                    prev["group_id"] = row.get("group_id")
        rows = list(by_fp.values()) + no_fp

    # ISP-aware order: when a ping-built per-ISP map exists (Rightel/…),
    # keep that map's relative order among this user's links — do NOT
    # re-bucket via category slots (that washed out the ISP arrangement
    # and made every network look the same). Otherwise: category slots
    # + global average ping rank.
    _rank_op = to_ping_operator(rank_operator) or None
    if _rank_op and has_operator_order_map(_rank_op):
        rows = sorted(
            rows,
            key=lambda row: link_order_index(
                str(row.get("link") or ""), operator=_rank_op
            ),
        )
    else:
        rows = arrange_subscription_rows(
            rows,
            ping_rank_of=lambda row: link_order_index(
                str(row.get("link") or ""), operator=_rank_op
            ),
        )

    # Normal clients: always fill up to SUB_MAX_CONFIGS with best-ranked first
    # (category slots + leftover). Probe/AstraPing keeps the full inventory.
    # Untested / unscored configs still pad the feed after scored peers.
    if not include_hidden:
        max_n = int(SUB_MAX_CONFIGS)
        if max_n > 0 and len(rows) > max_n:
            rows = rows[:max_n]

    renamed = apply_display_name(
        rows,
        getattr(user, "display_name", None),
        used=used,
        limit=limit,
        username=getattr(user, "username", None),
        user_status=user_status_s,
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
                        coro = adapter.get_usage(uname, rid, expand_links=True)  # type: ignore[call-arg]
                    except TypeError:
                        coro = adapter.get_usage(uname, rid)
                    res = await asyncio.wait_for(coro, timeout=10.0)
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


async def hydrate_share_links_pool_safe(user_id: int) -> int:
    """Hydrate share links without holding a pool connection during remote HTTP.

    Snapshot account/panel jobs in a short session, close it, fetch in parallel,
    then apply updates in a new short session. Never deletes rows.
    """
    from app.adapters.http_utils import (
        expand_subscription_links,
        is_share_link,
        is_subscription_ref,
        make_client,
    )
    from app.api.subscription import invalidate_user_feed_cache
    from app.core.database import AsyncSessionLocal

    jobs: list[dict] = []
    username = ""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User)
            .where(User.id == int(user_id))
            .options(
                selectinload(User.panel_accounts).selectinload(PanelAccount.panel),
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            return 0
        username = str(user.username or "")

        def _needs(account: PanelAccount) -> bool:
            if account.status not in (
                PanelAccountStatus.active,
                PanelAccountStatus.error,
                PanelAccountStatus.pending,
            ):
                return False
            if not account.panel or not _panel_is_active(account.panel):
                return False
            links = [
                str(x).strip() for x in (account.links or []) if x and str(x).strip()
            ]
            if any(is_subscription_ref(x) for x in links):
                return True
            if any(is_share_link(x) for x in links):
                return False
            return True

        for account in user.panel_accounts or []:
            if not _needs(account):
                continue
            panel = account.panel
            if panel is None:
                continue
            _touch_panel_for_detach(panel)
            stored = [
                str(x).strip() for x in (account.links or []) if x and str(x).strip()
            ]
            jobs.append(
                {
                    "account_id": int(account.id),
                    "panel": panel,
                    "stored": stored,
                    "uname": (account.remote_username or username or "").strip(),
                    "rid": account.remote_id
                    or account.remote_username
                    or (account.remote_username or username or None),
                    "prev_used": int(account.used_traffic or 0),
                }
            )
        await db.commit()

    if not jobs:
        return 0

    lock = await _lock_for_user(int(user_id))
    async with lock:
        sem = asyncio.Semaphore(max(4, min(12, len(jobs) + 2)))

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

        async def _fetch_one(job: dict) -> dict | None:
            async with sem:
                panel = job["panel"]
                aid = int(job["account_id"])
                stored = list(job["stored"] or [])
                before_shares = [x for x in stored if is_share_link(x)]
                before_n = len(before_shares)
                uname = job["uname"]
                rid = job["rid"]
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
                        coro = adapter.get_usage(uname, rid, expand_links=True)  # type: ignore[call-arg]
                    except TypeError:
                        coro = adapter.get_usage(uname, rid)
                    res = await asyncio.wait_for(coro, timeout=10.0)
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
                            res.used_traffic or job["prev_used"] or 0
                        ),
                    }
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "hydrate pool-safe failed user=%s panel=%s: %s",
                        username,
                        getattr(panel, "id", "?"),
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

        raw_results = await asyncio.gather(
            *[
                asyncio.wait_for(_fetch_one(j), timeout=16.0)
                for j in jobs
            ],
            return_exceptions=True,
        )

        updates = [x for x in raw_results if isinstance(x, dict) and x.get("links")]
        if not updates:
            return 0

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User)
                .where(User.id == int(user_id))
                .options(
                    selectinload(User.panel_accounts).selectinload(PanelAccount.panel),
                )
            )
            user = result.scalar_one_or_none()
            if user is None:
                return 0
            by_id = {int(a.id): a for a in (user.panel_accounts or [])}
            now = datetime.now(timezone.utc)
            updated = 0
            for item in updates:
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
                if account.panel is not None:
                    account.panel.last_ok_at = now
                updated += 1
            if updated:
                await db.commit()
                try:
                    invalidate_user_feed_cache(int(user_id))
                except Exception:  # noqa: BLE001
                    pass
            logger.info(
                "hydrate pool-safe user_id=%s jobs=%s updated=%s",
                user_id,
                len(jobs),
                updated,
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


def _live_remote_keys(accounts: list[PanelAccount]) -> set[tuple[int, str]]:
    """(panel_id, remote_username|remote_id) for active/error accounts."""
    keys: set[tuple[int, str]] = set()
    for account in accounts:
        if account.status not in (
            PanelAccountStatus.active,
            PanelAccountStatus.error,
        ):
            continue
        pid = int(account.panel_id)
        for raw_name in (
            (account.remote_username or "").strip().lower(),
            (account.remote_id or "").strip().lower(),
        ):
            if raw_name:
                keys.add((pid, raw_name))
    return keys


def _deleted_usage_superseded(
    account: PanelAccount, live_keys: set[tuple[int, str]]
) -> bool:
    """True when a live account already owns this remote counter.

    Group/panel reshuffles mark the old row ``deleted`` but keep ``used_traffic``.
    A new active row then re-pulls the same remote meter → without this check
    Astra would count the same bytes twice on the users page.
    """
    pid = int(account.panel_id)
    for raw_name in (
        (account.remote_username or "").strip().lower(),
        (account.remote_id or "").strip().lower(),
    ):
        if raw_name and (pid, raw_name) in live_keys:
            return True
    return False


def _account_remote_keys(account: PanelAccount) -> set[tuple[int, str]]:
    """(panel_id, remote username|id) keys for one account (may be empty)."""
    keys: set[tuple[int, str]] = set()
    pid = int(account.panel_id)
    for raw_name in (
        (account.remote_username or "").strip().lower(),
        (account.remote_id or "").strip().lower(),
    ):
        if raw_name:
            keys.add((pid, raw_name))
    return keys


def _live_raw_for_deleted(
    account: PanelAccount, accounts: list[PanelAccount]
) -> int:
    """Max raw on live/error twins that share this deleted row's remote identity."""
    keys = _account_remote_keys(account)
    if not keys:
        return 0
    best = 0
    for other in accounts:
        if other.status not in (
            PanelAccountStatus.active,
            PanelAccountStatus.error,
        ):
            continue
        if int(other.panel_id) != int(account.panel_id):
            continue
        if keys & _account_remote_keys(other):
            best = max(best, int(other.used_traffic or 0))
    return best


async def _weighted_total(
    accounts: list[PanelAccount], account_coeffs: dict[int, float]
) -> int:
    """Σ (raw_i × coeff_of_account_i's group).

    Deleted remotes still count when nothing replaced them (removing a config
    must not refund). If a live account on the same panel reuses the same
    remote username/id, only the extra deleted bytes beyond the live meter
    count — a fresh live twin at 0 must not wipe prior volume.
    """
    _sync_account_coeff_periods(accounts, account_coeffs)
    live_keys = _live_remote_keys(accounts)
    total = 0
    for account in accounts:
        if account.status in (
            PanelAccountStatus.active,
            PanelAccountStatus.error,
        ):
            coeff = _norm_coeff(account_coeffs.get(account.id, 1.0))
            total += account_weighted_usage(account, coeff)
            continue
        if account.status != PanelAccountStatus.deleted:
            continue
        raw = int(account.used_traffic or 0)
        if raw <= 0:
            continue
        stored = getattr(account, "applied_coefficient", None)
        coeff = _norm_coeff(
            stored if stored is not None else account_coeffs.get(account.id, 1.0)
        )
        if _deleted_usage_superseded(account, live_keys):
            # Live twin exists — only count bytes the live meter does not hold yet
            # (re-provision after remote wipe must not refund the deleted peak).
            live_raw = _live_raw_for_deleted(account, accounts)
            gap = raw - live_raw
            if gap <= 0:
                continue
            total += quota_math.weighted_bytes(gap, coeff)
            continue
        total += account_weighted_usage(account, coeff)
    return total


def _weighted_total_from_stored(accounts: list[PanelAccount]) -> int:
    """Fast Σ(raw×stored coeff) with no GroupPanel SQL — for quota-watch scans."""
    live_keys = _live_remote_keys(accounts)
    total = 0
    for account in accounts:
        if account.status in (
            PanelAccountStatus.active,
            PanelAccountStatus.error,
        ):
            coeff = _norm_coeff(getattr(account, "applied_coefficient", None) or 1.0)
            total += account_weighted_usage(account, coeff)
            continue
        if account.status != PanelAccountStatus.deleted:
            continue
        raw = int(account.used_traffic or 0)
        if raw <= 0:
            continue
        coeff = _norm_coeff(getattr(account, "applied_coefficient", None) or 1.0)
        if _deleted_usage_superseded(account, live_keys):
            live_raw = _live_raw_for_deleted(account, accounts)
            gap = raw - live_raw
            if gap <= 0:
                continue
            total += quota_math.weighted_bytes(gap, coeff)
            continue
        total += account_weighted_usage(account, coeff)
    return total


# Users who just opened /sub — quota watch pulls these first so «مصرف» moves live.
_LIVE_USAGE_PRIORITY: dict[int, float] = {}
# Cap concurrent usage pulls that hold a DB session across remote HTTP.
# Without this, dozens of bg/quota/admin pulls exhausted the pool and
# left sub «مصرف» stuck while the user was downloading.
# Cap concurrent remote usage pulls that hold ORM work. Was 2 — under /sub
# stampede each pull kept a pool connection open during remote HTTP and
# starved the panel with QueuePool TimeoutError → «Temporarily unavailable».
_USAGE_PULL_GATE = asyncio.Semaphore(1)


def mark_live_usage_priority(user_id: int) -> None:
    """Prefer this user on the next quota-watch remote pull wave."""
    uid = int(user_id)
    now = time.monotonic()
    _LIVE_USAGE_PRIORITY[uid] = now
    if len(_LIVE_USAGE_PRIORITY) > 4000:
        cutoff = now - 300.0
        for k, ts in list(_LIVE_USAGE_PRIORITY.items()):
            if ts < cutoff:
                _LIVE_USAGE_PRIORITY.pop(k, None)


def take_live_usage_priority(max_age_sec: float = 180.0, limit: int = 24) -> list[int]:
    """Newest live-sub users first (for quota-watch remote pulls)."""
    now = time.monotonic()
    items = [
        (uid, ts)
        for uid, ts in _LIVE_USAGE_PRIORITY.items()
        if now - float(ts) <= max_age_sec
    ]
    items.sort(key=lambda x: x[1], reverse=True)
    return [uid for uid, _ in items[: max(1, int(limit))]]


async def recount_user_used_traffic(
    db: AsyncSession, user: User, accounts: list[PanelAccount] | None = None
) -> int:
    """Recompute ``user.used_traffic`` from current panel accounts (no remote I/O).

    Never shrinks below the previous total — panel/config removal must not refund.
    Deleted meters that a live twin fully absorbs may be cleared; partial gaps stay.
    """
    if accounts is None:
        result = await db.execute(
            select(PanelAccount)
            .where(PanelAccount.user_id == user.id)
            .options(selectinload(PanelAccount.panel))
        )
        accounts = list(result.scalars().all())
    # Only clear deleted meters when a live twin already holds ≥ those bytes.
    # Never wipe a deleted peak just because a fresh live row exists at 0.
    live_keys = _live_remote_keys(accounts)
    for account in accounts:
        if account.status != PanelAccountStatus.deleted:
            continue
        raw = int(account.used_traffic or 0)
        if raw <= 0:
            continue
        if not _deleted_usage_superseded(account, live_keys):
            continue
        if _live_raw_for_deleted(account, accounts) >= raw:
            account.used_traffic = 0
    account_coeffs = await account_coefficients_for_user(db, user, accounts)
    total = await _weighted_total(accounts, account_coeffs)
    return _assign_user_used_traffic(user, total)


async def recount_users_used_traffic_batch(
    db: AsyncSession,
    users: list[User],
) -> list[int]:
    """Recount weighted usage; re-enforce quota when totals or limited/expired drift.

    Returns user ids whose status flipped (e.g. limited→active after superseded
    meters were dropped). Callers should unfreeze remotes for those ids.
    """
    flipped: list[int] = []
    for user in users:
        accounts = list(user.panel_accounts or [])
        before = int(user.used_traffic or 0)
        after = await recount_user_used_traffic(db, user, accounts)
        st = user.status
        # Always re-check when usage moved OR status is stuck limited/expired
        # with leftover quota (users-list recount used to skip enforce → false اتمام حجم).
        if after != before or st in (UserStatus.limited, UserStatus.expired):
            if await enforce_user_quota(db, user):
                flipped.append(int(user.id))
    return flipped


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
    lim = int(user.data_limit or 0)
    used = int(user.used_traffic or 0)
    used_ratio = (float(used) / float(lim)) if lim > 0 else 0.0
    # Leftover freeze marks after limited→active / volume top-up → must re-push
    if not blocked:
        from app.services.panel_usage_bulk import QUOTA_FROZEN_MARK

        if any(
            (a.error_message or "") == QUOTA_FROZEN_MARK for a in active
        ):
            return True
    return quota_math.needs_remote_cap_sync(
        status_changed=status_changed,
        blocked=blocked,
        force=force,
        active_panel_count=len(active),
        coefficients=[account_coeffs.get(a.id, 1.0) for a in active],
        shrink_shared_caps=shrink_shared_caps,
        used_ratio=used_ratio,
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

    quick=True → slightly shorter per-panel timeouts (admin list still
    covers every panel when force=True).
    isolated=True → each user uses its own short-lived session + commit
    (required for concurrent batch — shared AsyncSession drops updates).
    """
    if not users:
        return
    # Concurrent refresh MUST be isolated — AsyncSession is not task-safe.
    if concurrency > 1 and not isolated:
        isolated = True
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
        lim = int(u.data_limit or 0)
        used = int(u.used_traffic or 0)
        near_limit = lim > 0 and used >= int(lim * 0.80)
        # Quick path: still push when quota just flipped OR user is blocked
        # so every panel stays frozen after volume (with coeff) is exhausted.
        # Near-limit also pushes so remotes shrink ceilings before overshoot.
        if quick and not force and not changed and not blocked and not near_limit:
            return
        if (
            quick
            and force
            and not changed
            and not blocked
            and not near_limit
            and not push_remotes
        ):
            # Users-page sync: usage only — skip remote push unless quota flipped.
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
            force=bool(push_remotes) or blocked or near_limit,
            shrink_shared_caps=multi,
        ):
            await sync_modify_user(
                session,
                u,
                freeze_fast=blocked,
                skip_nginx_sync=True,
                remote_timeout=8.0 if blocked else 20.0,
            )

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
            # (2) optional remote push. Never hold a write lock across
            # panel HTTP inside sync_modify together with usage scrape.
            try:
                async with _USAGE_PULL_GATE:
                    used, activated, changed, status_now = (
                        await refresh_usage_pool_safe(
                            uid, force=force, quick=quick
                        )
                    )
                    if used < 0:
                        return
                    blocked = status_now in (
                        UserStatus.limited.value,
                        UserStatus.expired.value,
                        UserStatus.disabled.value,
                        "limited",
                        "expired",
                        "disabled",
                    )
                    # Re-read limit/used briefly for near_limit push decision.
                    lim_f = 0
                    used_f = int(used)
                    async with AsyncSessionLocal() as session:
                        row = (
                            await session.execute(
                                select(User.data_limit, User.used_traffic, User.status)
                                .where(User.id == uid)
                            )
                        ).one_or_none()
                        if row is None:
                            return
                        lim_f = int(row[0] or 0)
                        used_f = int(row[1] or 0)
                        st = row[2]
                        blocked = st in (
                            UserStatus.limited,
                            UserStatus.expired,
                            UserStatus.disabled,
                        )
                    near_limit = lim_f > 0 and used_f >= int(lim_f * 0.80)
                    need_push = True
                    if (
                        quick
                        and not force
                        and not changed
                        and not blocked
                        and not near_limit
                    ):
                        need_push = False
                    if (
                        quick
                        and force
                        and not changed
                        and not blocked
                        and not near_limit
                        and not push_remotes
                    ):
                        need_push = False
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
                    lim2 = int(fresh.data_limit or 0)
                    used2 = int(fresh.used_traffic or 0)
                    near2 = lim2 > 0 and used2 >= int(lim2 * 0.80)
                    if await should_push_remotes(
                        session,
                        fresh,
                        accounts,
                        status_changed=changed,
                        force=bool(push_remotes) or blocked or near2,
                        shrink_shared_caps=multi,
                    ):
                        await sync_modify_user(
                            session,
                            fresh,
                            freeze_fast=blocked,
                            skip_nginx_sync=True,
                            remote_timeout=8.0 if blocked else 20.0,
                        )
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
        # End the implicit read transaction BEFORE remote HTTP so the pool
        # connection is returned (idle-in-transaction was starving /sub).
        # expire_on_commit=False keeps account/panel instances usable in memory.
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
        # force (admin users page / detail): pull EVERY active|error remote.
        # quick alone (sub bg): fail-faster, but still cover all panels in waves —
        # never cancel half the configs because of a tiny flat timeout.
        pullable = [
            a
            for a in accounts
            if a.panel
            and _panel_is_active(a.panel)
            and a.panel.type != PanelType.astra
            and a.status
            in (
                PanelAccountStatus.active,
                PanelAccountStatus.error,
            )
            and (a.remote_username or a.remote_id or user.username)
        ]
        n_pull = len(pullable)
        # quick MUST win over force for budgets — otherwise bg/quota
        # force+quick still burned 56s+ and starved the DB pool (pulled=0).
        if quick:
            per_try = 3.0
            tries = 1
            panel_conc = 14
            waves = max(1, (n_pull + panel_conc - 1) // panel_conc) if n_pull else 1
            overall_budget = min(14.0, max(5.0, waves * (per_try + 0.35) + 1.5))
            # One short miss wave when force+quick (sub/quota) — not the 45s admin retry.
            do_miss_retry = bool(force)
        elif force:
            # Complete coverage — users page must not miss panels/groups.
            per_try = 10.0
            tries = 2
            panel_conc = 12
            waves = max(1, (n_pull + panel_conc - 1) // panel_conc) if n_pull else 1
            wave_budget = per_try * tries + 2.0
            # Soft ceiling only; unfinished remotes get a dedicated retry wave.
            overall_budget = min(95.0, max(20.0, waves * wave_budget + wave_budget))
            do_miss_retry = True
        else:
            per_try = 5.0
            tries = 1
            panel_conc = 10
            waves = max(1, (n_pull + panel_conc - 1) // panel_conc) if n_pull else 1
            overall_budget = min(40.0, max(12.0, waves * (per_try + 1.0) + 5.0))
            do_miss_retry = False

        panel_sem = asyncio.Semaphore(max(1, panel_conc))

        async def _remote(
            account: PanelAccount,
        ) -> tuple[PanelAccount, AdapterResult | None]:
            async with panel_sem:
                uname = (account.remote_username or user.username or "").strip()
                rid = account.remote_id or account.remote_username or uname or None
                last: AdapterResult | None = None
                for attempt in range(tries):
                    adapter = get_adapter(account.panel)
                    try:
                        try:
                            coro = adapter.get_usage(
                                uname, rid, expand_links=False
                            )  # type: ignore[call-arg]
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
                        await asyncio.sleep(0.12)
                return account, last

        async def _run_wave(
            subset: list[PanelAccount], *, budget: float
        ) -> list[tuple[PanelAccount, AdapterResult | None]]:
            if not subset:
                return []
            tasks = [asyncio.create_task(_remote(a)) for a in subset]
            # force: wait for the full wave (each call already has per_try).
            # Soft budget is a safety net only — unfinished go to retry wave.
            done, pending = await asyncio.wait(tasks, timeout=max(1.0, budget))
            cancelled = 0
            for t in pending:
                t.cancel()
                cancelled += 1
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            out: list[tuple[PanelAccount, AdapterResult | None]] = []
            for t in done:
                try:
                    out.append(t.result())
                except Exception:  # noqa: BLE001
                    pass
            if cancelled:
                logger.warning(
                    "refresh_usage wave timeout user=%s asked=%s got=%s cancelled=%s "
                    "budget=%.1f force=%s",
                    user.username,
                    len(subset),
                    len(out),
                    cancelled,
                    budget,
                    force,
                )
            return out

        outcomes: list[tuple[PanelAccount, AdapterResult | None]] = []
        if pullable:
            outcomes = await _run_wave(pullable, budget=overall_budget)
            if do_miss_retry:
                ok_ids = {
                    int(a.id)
                    for a, res in outcomes
                    if res is not None and res.success and a.id is not None
                }
                missed = [a for a in pullable if int(a.id) not in ok_ids]
                if missed:
                    # Second chance for panels that timed out or failed.
                    if quick:
                        retry_budget = min(
                            8.0,
                            max(3.0, ((len(missed) + panel_conc - 1) // panel_conc)
                            * (per_try + 0.5)),
                        )
                    else:
                        retry_budget = min(
                            45.0,
                            max(10.0, ((len(missed) + panel_conc - 1) // panel_conc)
                            * (per_try * tries + 2.0)),
                        )
                    outcomes.extend(await _run_wave(missed, budget=retry_budget))

        now = datetime.now(timezone.utc)
        from app.adapters.http_utils import is_share_link

        ok_n = 0
        seen_ok: set[int] = set()
        for account, res in outcomes:
            if res is None:
                continue
            if res.success:
                aid = int(account.id)
                if aid in seen_ok:
                    continue
                seen_ok.add(aid)
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
                # Do NOT touch panel.last_ok_at here — concurrent usage pulls
                # updating the same Panel row caused Postgres deadlocks and
                # aborted the weighted used_traffic commit.
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

        miss_n = max(0, n_pull - ok_n)
        logger.info(
            "refresh_usage user=%s panels=%s pullable=%s ok=%s miss=%s "
            "force=%s quick=%s budget=%s",
            user.username,
            len(accounts),
            n_pull,
            ok_n,
            miss_n,
            force,
            quick,
            overall_budget,
        )

    # Refresh coeffs in case new accounts appeared (should not, but cheap)
    account_coeffs = await account_coefficients_for_user(db, user, accounts)
    total = await _weighted_total(accounts, account_coeffs)
    total = _assign_user_used_traffic(user, total)
    await db.flush()
    return total


async def refresh_usage_pool_safe(
    user_id: int,
    *,
    force: bool = False,
    quick: bool = True,
) -> tuple[int, bool, bool, str]:
    """Like refresh_usage but never holds a pool connection during remote HTTP.

    Opens a short session to load + touch panel attrs, closes it (returns the
    connection), runs get_usage, then opens a new session to apply meters and
    recount. Returns ``(used, activated, quota_changed, status)``; used=-1 if
    the user is missing / not pullable.
    """
    from app.core.database import AsyncSessionLocal

    pullable: list[tuple[int, Panel, str, str | None, int]] = []
    username = ""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User)
            .where(User.id == int(user_id))
            .options(
                selectinload(User.panel_accounts).selectinload(PanelAccount.panel),
                selectinload(User.groups),
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            return (-1, False, False, "")
        if user.status not in (
            UserStatus.active,
            UserStatus.on_hold,
            UserStatus.limited,
        ):
            return (-1, False, False, str(getattr(user.status, "value", user.status)))
        username = str(user.username or "")
        # Inline Astra sync while we still have a session (local SQL, short).
        try:
            from app.services import astra_node as astra_node_svc

            astra_synced: set[int] = set()
            for account in user.panel_accounts or []:
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
                    pass
            for account in user.panel_accounts or []:
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
        except Exception:  # noqa: BLE001
            pass

        for account in user.panel_accounts or []:
            panel = account.panel
            if (
                not panel
                or not _panel_is_active(panel)
                or panel.type == PanelType.astra
                or account.status
                not in (PanelAccountStatus.active, PanelAccountStatus.error)
            ):
                continue
            uname = (account.remote_username or user.username or "").strip()
            if not uname and not account.remote_id:
                continue
            # Touch attrs so get_adapter works on the detached Panel after close.
            _ = (
                panel.id,
                panel.type,
                panel.name,
                panel.is_active,
                getattr(panel, "base_url", None),
                getattr(panel, "url", None),
                getattr(panel, "username", None),
                getattr(panel, "password", None),
                getattr(panel, "token", None),
                getattr(panel, "meta", None),
            )
            rid = account.remote_id or account.remote_username or uname or None
            pullable.append(
                (
                    int(account.id),
                    panel,
                    uname,
                    str(rid) if rid else None,
                    int(account.used_traffic or 0),
                )
            )
        await db.commit()
    # Session closed → pool connection returned before remote HTTP.

    per_try = 3.0 if quick else 10.0
    results: dict[int, AdapterResult] = {}
    sem = asyncio.Semaphore(8 if quick else 12)

    async def _one(
        aid: int, panel: Panel, uname: str, rid: str | None
    ) -> None:
        async with sem:
            adapter = get_adapter(panel)
            try:
                try:
                    coro = adapter.get_usage(uname, rid, expand_links=False)  # type: ignore[call-arg]
                except TypeError:
                    coro = adapter.get_usage(uname, rid)
                res = await asyncio.wait_for(coro, timeout=per_try)
                if res is not None and res.success:
                    results[aid] = res
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "pool-safe get_usage user=%s panel=%s: %s",
                    username,
                    panel.id,
                    _exc_msg(exc),
                )
            finally:
                close = getattr(adapter, "close", None)
                if callable(close):
                    try:
                        await close()
                    except Exception:  # noqa: BLE001
                        pass

    if pullable:
        await asyncio.gather(
            *[_one(aid, panel, uname, rid) for aid, panel, uname, rid, _prev in pullable]
        )

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User)
            .where(User.id == int(user_id))
            .options(
                selectinload(User.panel_accounts).selectinload(PanelAccount.panel),
                selectinload(User.groups),
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            return (-1, False, False, "")
        now = datetime.now(timezone.utc)
        from app.adapters.http_utils import is_share_link

        by_id = {int(a.id): a for a in (user.panel_accounts or [])}
        for aid, res in results.items():
            account = by_id.get(int(aid))
            if account is None:
                continue
            got = int(res.used_traffic or 0)
            prev = int(account.used_traffic or 0)
            panel = account.panel
            if panel is not None and panel.type == PanelType.astra:
                account.used_traffic = got
            else:
                account.used_traffic = max(prev, got)
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
            account.last_sync_at = now
            if account.status == PanelAccountStatus.error:
                account.status = PanelAccountStatus.active
                account.error_message = None
        accounts = list(user.panel_accounts or [])
        account_coeffs = await account_coefficients_for_user(db, user, accounts)
        total = await _weighted_total(accounts, account_coeffs)
        total = _assign_user_used_traffic(user, total)
        activated = await activate_on_hold_if_used(db, user)
        changed = bool(activated) or await enforce_user_quota(db, user)
        await db.commit()
        st = getattr(user.status, "value", user.status)
        return (int(user.used_traffic or total or 0), bool(activated), bool(changed), str(st))


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


async def sync_all_astra_traffic(db: AsyncSession | None = None) -> int:
    """Pull Xray stats for every active Astra panel.

    Each panel uses its own short session so concurrent quota/usage writers
    cannot deadlock on ``panels.meta`` updates.
    """
    from app.core.database import AsyncSessionLocal
    from app.services import astra_node as astra_node_svc

    async with AsyncSessionLocal() as list_db:
        result = await list_db.execute(
            select(Panel.id).where(
                Panel.type == PanelType.astra,
                Panel.is_active.is_(True),
            )
        )
        panel_ids = [int(x) for (x,) in result.all()]

    n = 0
    for pid in panel_ids:
        try:
            async with AsyncSessionLocal() as session:
                panel = (
                    await session.execute(select(Panel).where(Panel.id == pid))
                ).scalar_one_or_none()
                if panel is None:
                    continue
                await astra_node_svc.sync_traffic_from_xray(
                    session, panel, rebuild_on_limit=False
                )
                await session.commit()
                n += 1
        except Exception:  # noqa: BLE001
            logger.debug("astra traffic sync failed panel=%s", pid, exc_info=True)
    return n


async def _freeze_pasarguard_limited_accounts(
    *,
    per_panel_limit: int = 250,
    concurrency: int = 20,
    wall: float = 35.0,
) -> tuple[int, int]:
    """Panel-scoped PasarGuard disable for limited/expired users.

    Much faster than per-user sync_modify (one /disabled call, no group resolve).
    This is what actually stops leftover traffic on panels like moji.fusetea.
    """
    from app.core.database import AsyncSessionLocal
    from app.services.panel_usage_bulk import QUOTA_FROZEN_MARK

    t0 = time.monotonic()
    ok_n = 0
    fail_n = 0
    async with AsyncSessionLocal() as db:
        panels = list(
            (
                await db.execute(
                    select(Panel).where(
                        Panel.is_active.is_(True),
                        Panel.type == PanelType.pasarguard,
                    )
                )
            ).scalars().all()
        )
    for panel in panels:
        if time.monotonic() - t0 > wall:
            break
        async with AsyncSessionLocal() as db:
            panel_row = await db.get(Panel, int(panel.id))
            if panel_row is None or not panel_row.is_active:
                continue
            rows = list(
                (
                    await db.execute(
                        select(PanelAccount, User)
                        .join(User, User.id == PanelAccount.user_id)
                        .where(
                            PanelAccount.panel_id == int(panel.id),
                            User.status.in_(
                                (UserStatus.limited, UserStatus.expired)
                            ),
                            PanelAccount.status.in_(
                                (
                                    PanelAccountStatus.active,
                                    PanelAccountStatus.error,
                                    PanelAccountStatus.pending,
                                )
                            ),
                            # Skip already-confirmed freezes in SQL (not after LIMIT)
                            (PanelAccount.error_message.is_(None))
                            | (PanelAccount.error_message != QUOTA_FROZEN_MARK),
                        )
                        # Bleeders first: highest raw usage before id order
                        .order_by(
                            PanelAccount.used_traffic.desc().nullslast(),
                            User.id.desc(),
                        )
                        .limit(max(1, int(per_panel_limit)))
                    )
                ).all()
            )
            targets = list(rows)
            if not targets:
                continue
            # Skip whole panel if admin write is known-blocked (avoid hammering)
            meta0 = dict(panel_row.meta or {})
            if meta0.get("pg_admin_write_blocked"):
                # Re-check occasionally via first target only
                adapter_chk = get_adapter(panel_row)
                try:
                    probe = await adapter_chk.test_connection()  # type: ignore[attr-defined]
                    if not probe.success and "محدود" in (probe.error or ""):
                        fail_n += len(targets)
                        for acc, _u in targets:
                            acc.status = PanelAccountStatus.error
                            acc.error_message = (
                                "ادمین پنل پاسارگاد محدود شده — قطع کاربر ممکن نیست "
                                "(حجم ادمین پنل را زیاد کنید)"
                            )
                        await db.commit()
                        continue
                    if probe.success:
                        meta0.pop("pg_admin_write_blocked", None)
                        meta0.pop("pg_admin_write_blocked_at", None)
                        meta0.pop("pg_admin_write_blocked_err", None)
                        panel_row.meta = meta0
                except Exception:
                    fail_n += len(targets)
                    continue
                finally:
                    try:
                        await adapter_chk.close()  # type: ignore[attr-defined]
                    except Exception:
                        pass
            adapter = get_adapter(panel_row)
            sem = asyncio.Semaphore(max(1, concurrency))

            async def _one(acc: PanelAccount, user: User) -> bool:
                async with sem:
                    uname = (acc.remote_username or user.username or "").strip()
                    hint = (acc.remote_id or "").strip()
                    try:
                        uid = int(hint) if hint.isdigit() else None
                        if uid is None:
                            uid = await adapter._resolve_numeric_user_id(  # type: ignore[attr-defined]
                                uname, hint=hint or None
                            )
                        if uid is None:
                            acc.status = PanelAccountStatus.error
                            acc.error_message = (
                                "PasarGuard user id پیدا نشد؛ خاموش‌سازی ممکن نیست"
                            )
                            return False
                        res = await adapter._set_disabled_by_id(uid, True)  # type: ignore[attr-defined]
                        if res.success:
                            acc.remote_id = str(uid)
                            acc.status = PanelAccountStatus.active
                            acc.error_message = QUOTA_FROZEN_MARK
                            acc.last_sync_at = datetime.now(timezone.utc)
                            return True
                        err_l = (res.error or "").lower()
                        # Already gone on remote ⇒ traffic already cut
                        if any(
                            x in err_l
                            for x in ("not found", "یافت نشد", "وجود ندارد")
                        ):
                            acc.status = PanelAccountStatus.active
                            acc.error_message = QUOTA_FROZEN_MARK
                            acc.last_sync_at = datetime.now(timezone.utc)
                            return True
                        if (
                            "admin is limited" in err_l
                            or "write actions blocked" in err_l
                        ):
                            meta = dict(panel_row.meta or {})
                            meta["pg_admin_write_blocked"] = True
                            meta["pg_admin_write_blocked_at"] = datetime.now(
                                timezone.utc
                            ).isoformat()
                            panel_row.meta = meta
                            acc.status = PanelAccountStatus.error
                            acc.error_message = (
                                "ادمین پنل پاسارگاد محدود شده — قطع کاربر ممکن نیست "
                                "(حجم ادمین پنل را زیاد کنید)"
                            )
                            return False
                        acc.status = PanelAccountStatus.error
                        acc.error_message = (
                            f"خاموش‌سازی پاسارگاد ناموفق: {res.error or 'unknown'}"
                        )
                        return False
                    except Exception as exc:  # noqa: BLE001
                        acc.status = PanelAccountStatus.error
                        acc.error_message = (
                            str(exc).strip() or exc.__class__.__name__
                        )[:300]
                        return False

            results = await asyncio.gather(
                *[_one(a, u) for a, u in targets],
                return_exceptions=True,
            )
            for r in results:
                if r is True:
                    ok_n += 1
                else:
                    fail_n += 1
            try:
                await db.commit()
            except Exception:  # noqa: BLE001
                logger.debug("pg freeze commit failed panel=%s", panel.id, exc_info=True)
            try:
                await adapter.close()  # type: ignore[attr-defined]
            except Exception:
                pass
    return ok_n, fail_n


async def _freeze_quota_users(
    user_ids: list[int],
    *,
    remote_timeout: float = 8.0,
    concurrency: int = 16,
    wall: float = 45.0,
) -> tuple[int, int]:
    """Disable every remote config for exhausted users (PasarGuard /disabled first).

    Earlier ~1s wall cancelled freezes before PasarGuard disable finished, so
    Astra showed اتمام حجم while remotes stayed online.
    """
    from app.core.database import AsyncSessionLocal

    ids = list(dict.fromkeys(int(x) for x in user_ids if x))
    if not ids:
        return 0, 0
    sem = asyncio.Semaphore(max(1, concurrency))
    ok_n = 0
    fail_n = 0

    async def _one(uid: int) -> bool:
        async with sem:
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
                    return True
                if user.status not in (
                    UserStatus.limited,
                    UserStatus.expired,
                    UserStatus.disabled,
                ):
                    return True
                await sync_modify_user(
                    db,
                    user,
                    remote_timeout=remote_timeout,
                    skip_nginx_sync=True,
                    freeze_fast=True,
                )
                fail = any(
                    a.status == PanelAccountStatus.error
                    for a in (user.panel_accounts or [])
                    if a.status != PanelAccountStatus.deleted
                    and (a.error_message or "") != "ASTRA_QUOTA_FROZEN"
                )
                await db.commit()
                return not fail

    tasks = [asyncio.create_task(_one(uid)) for uid in ids]
    done, pending = await asyncio.wait(tasks, timeout=wall)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        fail_n += len(pending)
    for t in done:
        try:
            if t.result():
                ok_n += 1
            else:
                fail_n += 1
        except Exception:  # noqa: BLE001
            fail_n += 1
    return ok_n, fail_n


async def _quota_pull_users(
    user_ids: list[int],
    *,
    deadline: float,
    stats: dict[str, int],
    flipped_ids: list[int],
    activate_ids: list[int],
) -> list[int]:
    """Pull all remotes for each user, apply coeffs, enforce, return new flips."""
    from app.core.database import AsyncSessionLocal

    extra_flip: list[int] = []
    for uid in user_ids:
        if time.monotonic() > deadline:
            break
        try:
            async with _USAGE_PULL_GATE:
                used, activated, changed, _st = await refresh_usage_pool_safe(
                    int(uid), force=True, quick=True
                )
                if used < 0:
                    continue
                stats["usage_pulled"] += 1
                if activated and int(uid) not in activate_ids:
                    activate_ids.append(int(uid))
                if changed and int(uid) not in flipped_ids:
                    flipped_ids.append(int(uid))
                    extra_flip.append(int(uid))
                    stats["flipped"] += 1
                # Exhausted → freeze every remote in this same cycle
                async with AsyncSessionLocal() as db:
                    user = (
                        await db.execute(select(User).where(User.id == int(uid)))
                    ).scalar_one_or_none()
                    if user is not None and user.status in (
                        UserStatus.limited,
                        UserStatus.expired,
                        UserStatus.disabled,
                    ):
                        if int(user.id) not in flipped_ids:
                            flipped_ids.append(int(user.id))
                        if int(user.id) not in extra_flip:
                            extra_flip.append(int(user.id))
                        # Immediate hard cut in this pull — do not wait for
                        # the later batch freeze (late cut left remotes online).
                        try:
                            await sync_modify_user(
                                db,
                                user,
                                remote_timeout=10.0,
                                skip_nginx_sync=True,
                                freeze_fast=True,
                            )
                            await db.commit()
                            # #region agent log
                            try:
                                import json as _json

                                with open(
                                    "/opt/astra/data/debug-941288.ndjson",
                                    "a",
                                    encoding="utf-8",
                                ) as f:
                                    f.write(
                                        _json.dumps(
                                            {
                                                "sessionId": "941288",
                                                "runId": "freeze-immediate",
                                                "hypothesisId": "H-J",
                                                "location": "orchestrator._quota_pull_users",
                                                "message": "immediate_freeze",
                                                "data": {
                                                    "user_id": int(user.id),
                                                    "username": user.username,
                                                    "used": int(
                                                        user.used_traffic or 0
                                                    ),
                                                    "limit": int(
                                                        user.data_limit or 0
                                                    ),
                                                    "status": getattr(
                                                        user.status,
                                                        "value",
                                                        str(user.status),
                                                    ),
                                                },
                                                "timestamp": int(
                                                    time.time() * 1000
                                                ),
                                            },
                                            ensure_ascii=False,
                                        )
                                        + "\n"
                                    )
                            except Exception:
                                pass
                            # #endregion
                        except Exception:  # noqa: BLE001
                            logger.debug(
                                "immediate freeze failed user_id=%s",
                                uid,
                                exc_info=True,
                            )
                    elif (
                        int(user.data_limit or 0) > 0
                        and int(user.used_traffic or 0)
                        >= int(int(user.data_limit or 0) * 0.80)
                    ):
                        # Near-limit: shrink remote ceilings before overshoot
                        try:
                            await sync_modify_user(
                                db,
                                user,
                                remote_timeout=12.0,
                                skip_nginx_sync=True,
                            )
                            await db.commit()
                        except Exception:  # noqa: BLE001
                            logger.debug(
                                "near-limit cap push failed user_id=%s",
                                uid,
                                exc_info=True,
                            )
        except Exception:  # noqa: BLE001
            logger.debug("usage pull failed user_id=%s", uid, exc_info=True)
    return extra_flip


async def run_quota_watch_cycle() -> dict[str, int]:
    """Scalable quota loop: bulk panel sync → priority pulls → freeze.

    Designed for large installs (100k–1M+ subs): never ORM-scan thousands of
    users each cycle. Bulk GET /api/users keeps counters accurate; exhausted
    users are frozen on every remote in the same cycle.
    """
    from app.core.database import AsyncSessionLocal
    from app.services import panel_usage_bulk as bulk_usage

    t0 = time.monotonic()
    try:
        await heal_users_missing_panel_accounts(limit_users=6)
    except Exception:  # noqa: BLE001
        logger.debug("quota-watch missing-account heal skipped", exc_info=True)

    stats = {
        "astra_panels": 0,
        "checked": 0,
        "flipped": 0,
        "freeze_ok": 0,
        "freeze_fail": 0,
        "usage_pulled": 0,
        "bulk_listed": 0,
        "bulk_dirty": 0,
    }
    flipped_ids: list[int] = []
    activate_ids: list[int] = []

    # --- Phase 0: live /sub clients first (accurate while downloading) ---
    recent_sub_ids: list[int] = []
    try:
        from datetime import datetime, timedelta, timezone

        from app.models.user_analytics import UserUsageState

        since = datetime.now(timezone.utc) - timedelta(minutes=20)
        async with AsyncSessionLocal() as db:
            recent_sub_ids = [
                int(uid)
                for (uid,) in (
                    await db.execute(
                        select(UserUsageState.user_id)
                        .where(UserUsageState.last_sub_fetch_at.is_not(None))
                        .where(UserUsageState.last_sub_fetch_at >= since)
                        .order_by(UserUsageState.last_sub_fetch_at.desc())
                        .limit(50)
                    )
                ).all()
            ]
    except Exception:  # noqa: BLE001
        logger.debug("quota-watch recent sub list failed", exc_info=True)

    priority_ids = list(
        dict.fromkeys(take_live_usage_priority(180.0, 30) + recent_sub_ids)
    )[:10]
    extra_flip = await _quota_pull_users(
        priority_ids,
        deadline=t0 + 24.0,
        stats=stats,
        flipped_ids=flipped_ids,
        activate_ids=activate_ids,
    )
    if extra_flip:
        ok_n, fail_n = await _freeze_quota_users(extra_flip)
        stats["freeze_ok"] += ok_n
        stats["freeze_fail"] += fail_n

    # --- Phase 1: local Astra Xray (isolated sessions — no meta deadlocks) ---
    try:
        stats["astra_panels"] = await sync_all_astra_traffic()
    except Exception:  # noqa: BLE001
        logger.debug("quota-watch astra sync failed", exc_info=True)

    # --- Phase 2: bulk Marzban/PasarGuard/Rebecca /api/users (scales) ---
    try:
        bulk = await bulk_usage.sync_panels_bulk_rotate(max_panels=2)
        stats["bulk_listed"] = int(bulk.get("listed") or 0)
        stats["bulk_dirty"] = int(bulk.get("dirty") or 0)
        stats["checked"] += int(bulk.get("dirty") or 0)
        stats["flipped"] += int(bulk.get("flipped") or 0)
    except Exception:  # noqa: BLE001
        logger.warning("quota-watch bulk sync failed", exc_info=True)

    # --- Phase 3a: false «اتمام حجم» (limited but used < limit) → active + unfreeze ---
    try:
        false_lim = await bulk_usage.find_false_limited_user_ids(limit=80)
    except Exception:  # noqa: BLE001
        false_lim = []
    if false_lim:
        try:
            healed = await bulk_usage.recount_users_usage(false_lim, concurrency=8)
            stats["flipped"] += len(healed)
            # Push remotes for anyone now active again (unfreeze)
            reactivate = []
            async with AsyncSessionLocal() as db:
                for uid in false_lim:
                    u = await db.get(User, int(uid))
                    if u is not None and u.status == UserStatus.active:
                        reactivate.append(int(uid))
            for uid in reactivate[:40]:
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
                        if user is None or user.status != UserStatus.active:
                            continue
                        await sync_modify_user(
                            db,
                            user,
                            remote_timeout=20.0,
                            skip_nginx_sync=True,
                        )
                        await db.commit()
                        stats["freeze_ok"] += 1
                except Exception:  # noqa: BLE001
                    stats["freeze_fail"] += 1
                    logger.debug(
                        "quota-watch unfreeze failed user_id=%s", uid, exc_info=True
                    )
        except Exception:  # noqa: BLE001
            logger.warning("quota-watch false-limited heal failed", exc_info=True)

    # --- Phase 3a2: active + remain but remotes still ASTRA_QUOTA_FROZEN ---
    try:
        stuck_frozen = await bulk_usage.find_active_needing_unfreeze_ids(limit=40)
    except Exception:  # noqa: BLE001
        stuck_frozen = []
    for uid in stuck_frozen:
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(User)
                    .where(User.id == int(uid))
                    .options(
                        selectinload(User.panel_accounts).selectinload(
                            PanelAccount.panel
                        )
                    )
                )
                user = result.scalar_one_or_none()
                if user is None or user.status != UserStatus.active:
                    continue
                await sync_modify_user(
                    db,
                    user,
                    remote_timeout=20.0,
                    skip_nginx_sync=True,
                )
                await db.commit()
                stats["freeze_ok"] += 1
                logger.info(
                    "quota-watch unfreeze stuck-frozen user=%s id=%s",
                    user.username,
                    uid,
                )
        except Exception:  # noqa: BLE001
            stats["freeze_fail"] += 1
            logger.debug(
                "quota-watch stuck-frozen unfreeze failed user_id=%s",
                uid,
                exc_info=True,
            )

    # --- Phase 3b: PasarGuard panel-scoped disable (fast) + per-user freeze ---
    try:
        pg_ok, pg_fail = await _freeze_pasarguard_limited_accounts(
            per_panel_limit=220,
            concurrency=18,
            wall=32.0,
        )
        stats["freeze_ok"] += pg_ok
        stats["freeze_fail"] += pg_fail
    except Exception:  # noqa: BLE001
        logger.warning("quota-watch pasarguard freeze pass failed", exc_info=True)

    try:
        exhausted = await bulk_usage.find_exhausted_user_ids(limit=120)
    except Exception:  # noqa: BLE001
        exhausted = []
    try:
        need_freeze = await bulk_usage.find_limited_needing_freeze_ids(limit=80)
    except Exception:  # noqa: BLE001
        need_freeze = []
    freeze_ids = list(dict.fromkeys(flipped_ids + exhausted + need_freeze))[:60]
    if freeze_ids:
        # Ensure status is limited before freeze push
        try:
            more = await bulk_usage.recount_users_usage(freeze_ids, concurrency=8)
            freeze_ids = list(dict.fromkeys(freeze_ids + more))
            stats["flipped"] += len(more)
        except Exception:  # noqa: BLE001
            pass
        ok_n, fail_n = await _freeze_quota_users(
            freeze_ids, remote_timeout=8.0, concurrency=12, wall=28.0
        )
        stats["freeze_ok"] += ok_n
        stats["freeze_fail"] += fail_n

    # --- Phase 4: near-limit + on_hold targeted pulls ---
    try:
        near_ids = await bulk_usage.find_near_limit_user_ids(limit=40)
    except Exception:  # noqa: BLE001
        near_ids = []
    hold_ids: list[int] = []
    try:
        async with AsyncSessionLocal() as db:
            hold_ids = [
                int(x)
                for (x,) in (
                    await db.execute(
                        select(User.id)
                        .where(User.status == UserStatus.on_hold)
                        .order_by(User.id.desc())
                        .limit(30)
                    )
                ).all()
            ]
    except Exception:  # noqa: BLE001
        pass

    already = set(priority_ids) | set(freeze_ids)
    pull_ids = [
        uid
        for uid in dict.fromkeys(near_ids + hold_ids)
        if uid not in already
    ][:8]
    if pull_ids and time.monotonic() - t0 < 50.0:
        more_flip = await _quota_pull_users(
            pull_ids,
            deadline=t0 + 58.0,
            stats=stats,
            flipped_ids=flipped_ids,
            activate_ids=activate_ids,
        )
        if more_flip:
            ok_n, fail_n = await _freeze_quota_users(more_flip)
            stats["freeze_ok"] += ok_n
            stats["freeze_fail"] += fail_n

    if time.monotonic() - t0 < 55.0:
        for uid in list(dict.fromkeys(activate_ids))[:6]:
            if time.monotonic() - t0 > 60.0:
                break
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
                        db,
                        user,
                        remote_timeout=1.2,
                        skip_nginx_sync=True,
                        freeze_fast=True,
                    )
                    await db.commit()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "on_hold activate push failed user_id=%s", uid, exc_info=True
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
    # populate_existing: after parallel delete workers commit, this session must
    # not reuse stale panel_accounts collections that omit deleted meters.
    result = await db.execute(
        select(User)
        .where(User.id.in_(user_ids))
        .options(selectinload(User.panel_accounts))
        .execution_options(populate_existing=True)
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
        _assign_user_used_traffic(
            user, await _weighted_total(accounts, account_coeffs)
        )
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


async def category_group_ids(db: AsyncSession, category_id: int) -> list[int]:
    """Enabled sales groups currently in a category (stable id order)."""
    from app.models.group import Group

    rows = await db.execute(
        select(Group.id)
        .where(
            Group.category_id == int(category_id),
            Group.is_disabled.is_(False),
        )
        .order_by(Group.id.asc())
    )
    return [int(r[0]) for r in rows.all() if r[0] is not None]


async def apply_user_group_membership(
    db: AsyncSession,
    user: User,
    wanted_group_ids: list[int],
) -> bool:
    """Replace user groups + remotes to match ``wanted_group_ids``.

    Same safety as admin edit: delete remotes for removed groups first, then
    rewrite membership, provision new panels, hydrate links. Does NOT reset
    used_traffic / coeff anchors — only membership changes.

    ``wanted_group_ids=[]`` clears every group: remotes deleted, UserGroup wiped
    (needed when the last enabled group leaves a bound category).

    Returns True if membership actually changed.
    """
    from sqlalchemy import delete as sa_delete

    uid = int(user.id)
    uname = str(getattr(user, "username", "") or f"#{uid}")

    ordered: list[int] = []
    seen: set[int] = set()
    for raw in wanted_group_ids:
        try:
            gid = int(raw)
        except (TypeError, ValueError):
            continue
        if gid in seen:
            continue
        seen.add(gid)
        ordered.append(gid)

    ug = await db.execute(
        select(UserGroup.group_id)
        .where(UserGroup.user_id == uid)
        .order_by(UserGroup.sort_order.asc(), UserGroup.id.asc())
    )
    old_order = [int(row[0]) for row in ug.all() if row[0] is not None]
    if old_order == ordered:
        return False

    old_set = set(old_order)
    removed = sorted(old_set - set(ordered))

    if removed:
        # thorough remote cut (not fast) so main-panel configs actually leave
        results = await sync_delete_user_group_accounts(
            db, user, removed, fast=False
        )
        failed = [r for r in results if not r.get("ok")]
        if failed:
            # Soft: keep going for category bulk sync — log and skip hard fail
            # Local rows are still marked deleted; used_traffic is preserved.
            logger.warning(
                "category membership delete remotes partial user=%s failed=%s",
                uname,
                len(failed),
            )

    await db.execute(sa_delete(UserGroup).where(UserGroup.user_id == uid))
    for i, gid in enumerate(ordered):
        db.add(UserGroup(user_id=uid, group_id=gid, sort_order=i))
    await db.flush()

    added = [g for g in ordered if g not in old_set]
    if added:
        panels = await resolve_panels_for_groups(db, added)
        if panels:
            # Longer wait so category-bound adds actually land remotes/links
            # before the next user (was default ~2.5s → thin pending subs).
            await provision_users_now(db, [user], panels, wait=14.0)
            try:
                await db.refresh(user)
            except Exception:  # noqa: BLE001
                reloaded = await db.get(User, uid)
                if reloaded is not None:
                    user = reloaded
            try:
                await retry_failed_accounts(db, user)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "membership retry after add user=%s failed",
                    uname,
                    exc_info=True,
                )
    try:
        from app.api.subscription import invalidate_user_feed_cache

        invalidate_user_feed_cache(uid)
    except Exception:  # noqa: BLE001
        pass
    return True


async def deprovision_group_from_all_users(
    db: AsyncSession, group_id: int
) -> dict:
    """Delete every remote account for this sales group, then wipe UserGroup rows.

    Must run BEFORE the Group row is deleted — PanelAccount.group_id is SET NULL
    by FK and would otherwise skip remote cleanup.

    Bulk SQL only — never selectinload every member's accounts (large groups
    were timing out the admin delete UI at ~20s when a panel was already gone).
    """
    from app.api.subscription import invalidate_user_feed_cache
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import update as sa_update

    gid = int(group_id)
    ug = await db.execute(
        select(UserGroup.user_id).where(UserGroup.group_id == gid).distinct()
    )
    pa = await db.execute(
        select(PanelAccount.user_id)
        .where(
            PanelAccount.group_id == gid,
            PanelAccount.status != PanelAccountStatus.deleted,
        )
        .distinct()
    )
    uids = sorted(
        {
            int(r[0])
            for r in list(ug.all()) + list(pa.all())
            if r[0] is not None
        }
    )

    # Sub feed first: clear links + mark deleted in one UPDATE … RETURNING
    cut_result = await db.execute(
        sa_update(PanelAccount)
        .where(
            PanelAccount.group_id == gid,
            PanelAccount.status != PanelAccountStatus.deleted,
        )
        .values(
            links=[],
            links_by_group={},
            status=PanelAccountStatus.deleted,
            error_message=None,
        )
        .returning(PanelAccount.id)
    )
    cut_ids = [int(r[0]) for r in cut_result.all()]

    await db.execute(sa_delete(UserGroup).where(UserGroup.group_id == gid))
    await db.flush()

    # Bank deleted meters into each user before anything else can under-count.
    for uid in uids:
        try:
            user = await db.get(User, int(uid))
            if user is not None:
                await recount_user_used_traffic(db, user)
        except Exception:  # noqa: BLE001
            logger.warning(
                "deprovision-group recount failed user_id=%s", uid, exc_info=True
            )
    if uids:
        try:
            await db.flush()
        except Exception:  # noqa: BLE001
            pass

    try:
        if uids:
            invalidate_user_feed_cache(*uids)
    except Exception:  # noqa: BLE001
        pass

    if cut_ids:
        try:
            asyncio.create_task(
                _bg_cut_remote_accounts(cut_ids),
                name=f"astra-cut-g{gid}",
            )
        except RuntimeError:
            await _bg_cut_remote_accounts(cut_ids)

    stats = {"users": len(uids), "ok": len(cut_ids), "fail": 0}
    logger.info(
        "deprovision-group=%s users=%s local=%s remote-bg=%s",
        gid,
        stats["users"],
        stats["ok"],
        len(cut_ids),
    )
    return stats


async def _bg_cut_remote_accounts(account_ids: list[int]) -> None:
    """Delete remotes in parallel after Astra already dropped sub links."""
    from app.core.database import AsyncSessionLocal
    from app.services.ops_progress import bump_job, finish_job_id, start_job

    ids = [int(x) for x in account_ids if x]
    if not ids:
        return
    job_id = start_job(
        "delete_remotes",
        f"حذف از پنل‌ها ({len(ids)} اکانت)",
        total=len(ids),
        message="در حال حذف/قطع اکانت‌ها از پنل اصلی…",
    )
    # Cap remotes; never hold a DB checkout during HTTP (pool starve).
    sem = asyncio.Semaphore(8)

    async def _one(aid: int) -> None:
        async with sem:
            try:
                panel = None
                uname = ""
                rid = None
                uuid_val = None
                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(PanelAccount)
                        .where(PanelAccount.id == aid)
                        .options(selectinload(PanelAccount.panel))
                    )
                    acc = result.scalar_one_or_none()
                    if acc is None or not acc.panel:
                        return
                    uname = (acc.remote_username or "").strip()
                    if not uname:
                        return
                    panel = _touch_panel_for_detach(acc.panel)
                    rid = acc.remote_id or acc.remote_username
                    uuid_val = acc.uuid
                # Session closed before remote HTTP.
                adapter = get_adapter(panel)
                try:
                    await asyncio.wait_for(
                        adapter.delete_user(uname, rid),
                        timeout=1.4,
                    )
                except Exception:  # noqa: BLE001
                    try:
                        payload = UserPayload(
                            username=uname,
                            status="disabled",
                            data_limit=1,
                            expire=1,
                            uuid=uuid_val,
                            config={
                                "_remote_id": (str(rid or "")).strip(),
                            },
                        )
                        await asyncio.wait_for(
                            adapter.modify_user(payload), timeout=1.2
                        )
                    except Exception:  # noqa: BLE001
                        pass
                finally:
                    try:
                        await adapter.close()  # type: ignore[attr-defined]
                    except Exception:
                        pass
                bump_job(job_id, ok=True)
            except Exception:  # noqa: BLE001
                bump_job(job_id, ok=False)
                logger.debug("bg cut account=%s failed", aid, exc_info=True)

    await asyncio.gather(*[_one(i) for i in ids], return_exceptions=True)
    finish_job_id(
        job_id,
        ok=len(ids),
        message=f"حذف از پنل‌ها تمام شد — {len(ids)} اکانت",
    )
    logger.info("bg cut remotes done n=%s", len(ids))


async def _users_with_exact_groups(
    db: AsyncSession, group_ids: list[int]
) -> list[User]:
    """Users whose membership is exactly ``group_ids`` (order ignored)."""
    from app.models.user import User as UserModel

    want = sorted({int(g) for g in group_ids if g is not None})
    if not want:
        return []
    # Candidates that have every wanted group
    q = (
        select(UserGroup.user_id)
        .where(UserGroup.group_id.in_(want))
        .group_by(UserGroup.user_id)
        .having(func.count(UserGroup.group_id) == len(want))
    )
    cand_ids = [int(r[0]) for r in (await db.execute(q)).all() if r[0] is not None]
    if not cand_ids:
        return []
    # Exclude anyone who also has a group outside ``want``
    extra = await db.execute(
        select(UserGroup.user_id)
        .where(
            UserGroup.user_id.in_(cand_ids),
            UserGroup.group_id.notin_(want),
        )
        .distinct()
    )
    skip = {int(r[0]) for r in extra.all() if r[0] is not None}
    keep = [i for i in cand_ids if i not in skip]
    if not keep:
        return []
    result = await db.execute(
        select(UserModel)
        .where(UserModel.id.in_(keep))
        .options(
            selectinload(UserModel.panel_accounts).selectinload(PanelAccount.panel)
        )
    )
    return list(result.scalars().unique().all())


async def _users_whose_groups_subset_of(
    db: AsyncSession, allowed: set[int]
) -> list[User]:
    """Users whose entire membership sits inside ``allowed`` (non-empty).

    Picks up existing sales users created from a category even if
    ``source_category_id`` was never set.
    """
    from app.models.user import User as UserModel

    allow = {int(g) for g in allowed if g is not None}
    if not allow:
        return []
    have = await db.execute(
        select(UserGroup.user_id)
        .where(UserGroup.group_id.in_(list(allow)))
        .distinct()
    )
    cand = [int(r[0]) for r in have.all() if r[0] is not None]
    if not cand:
        return []
    extra = await db.execute(
        select(UserGroup.user_id)
        .where(
            UserGroup.user_id.in_(cand),
            UserGroup.group_id.notin_(list(allow)),
        )
        .distinct()
    )
    skip = {int(r[0]) for r in extra.all() if r[0] is not None}
    keep = [i for i in cand if i not in skip]
    if not keep:
        return []
    result = await db.execute(
        select(UserModel)
        .where(UserModel.id.in_(keep))
        .options(
            selectinload(UserModel.panel_accounts).selectinload(PanelAccount.panel)
        )
    )
    return list(result.scalars().unique().all())


async def resync_users_for_category(
    db: AsyncSession,
    category_id: int,
    *,
    removed_group_ids: list[int] | None = None,
    added_group_ids: list[int] | None = None,
    job_id: str | None = None,
) -> int:
    """Sync existing users of this category to current groups.

    Adds new groups on remotes + sub; removes dropped groups by cutting
    remotes fast. used_traffic on retired accounts is kept so Astra quota
    does not refund.

    Includes ``source_category_id`` users and anyone whose membership is a
    subset of this category (legacy users created before the bind existed).
    """
    from app.api.subscription import invalidate_user_feed_cache
    from app.models.group import GroupCategory
    from app.models.user import User as UserModel
    from app.services.ops_progress import bump_job, finish_job_id, start_job, update_job

    cid = int(category_id)
    cat = await db.get(GroupCategory, cid)
    if cat is None:
        if job_id:
            finish_job_id(job_id, message="دسته پیدا نشد")
        return 0

    cat_name = str(getattr(cat, "name", "") or f"#{cid}")
    force_removed = sorted(
        {int(g) for g in (removed_group_ids or []) if g is not None}
    )
    added = sorted({int(g) for g in (added_group_ids or []) if g is not None})

    if not job_id:
        if added and not force_removed:
            title = f"افزودن گروه به ساب کاربران دسته «{cat_name}»"
        elif force_removed and not added:
            title = f"حذف گروه از ساب کاربران دسته «{cat_name}»"
        else:
            title = f"همگام‌سازی ساب کاربران دسته «{cat_name}»"
        job_id = start_job(
            "category_resync",
            title,
            message="در حال پیدا کردن کاربران دسته…",
        )
    else:
        update_job(job_id, message="در حال پیدا کردن کاربران دسته…")

    desired = await category_group_ids(db, cid)

    result = await db.execute(
        select(UserModel)
        .where(UserModel.source_category_id == cid)
        .options(
            selectinload(UserModel.panel_accounts).selectinload(PanelAccount.panel)
        )
    )
    def _skip_sample(u: User) -> bool:
        name = str(u.username or "")
        return name.startswith("ag") or "نمونه کانفیگ" in (u.note or "")

    users_by_id: dict[int, User] = {}
    for u in result.scalars().unique().all():
        if _skip_sample(u):
            continue
        users_by_id[int(u.id)] = u

    orbit = set(desired) | set(force_removed) | set(added)
    if orbit:
        for u in await _users_whose_groups_subset_of(db, orbit):
            if _skip_sample(u):
                continue
            if int(u.id) in users_by_id:
                continue
            other = getattr(u, "source_category_id", None)
            if other is not None and int(other) != cid:
                continue
            if other is None:
                u.source_category_id = cid
            users_by_id[int(u.id)] = u

    if added and desired:
        prev = sorted(set(desired) - set(added))
        if prev:
            for u in await _users_with_exact_groups(db, prev):
                if _skip_sample(u):
                    continue
                if int(u.id) in users_by_id:
                    continue
                if getattr(u, "source_category_id", None) is None:
                    u.source_category_id = cid
                users_by_id[int(u.id)] = u

    # Anyone already holding an enabled group of this category (source_category
    # may be unset after legacy imports / manual group edits).
    hold_scope = sorted(set(desired) | set(force_removed))
    if hold_scope:
        hold_rows = await db.execute(
            select(UserGroup.user_id)
            .where(UserGroup.group_id.in_(hold_scope))
            .distinct()
        )
        hold_ids = [int(r[0]) for r in hold_rows.all() if r[0] is not None]
        missing_hold = [i for i in hold_ids if i not in users_by_id]
        if missing_hold:
            hold_users = await db.execute(
                select(UserModel)
                .where(UserModel.id.in_(missing_hold))
                .options(
                    selectinload(UserModel.panel_accounts).selectinload(
                        PanelAccount.panel
                    )
                )
            )
            for u in hold_users.scalars().unique().all():
                if _skip_sample(u):
                    continue
                other = getattr(u, "source_category_id", None)
                if other is not None and int(other) != cid:
                    continue
                if other is None:
                    u.source_category_id = cid
                users_by_id[int(u.id)] = u

    # Persist source_category_id backfills BEFORE expunge — otherwise legacy
    # discovery never sticks and later group waves miss those buyers.
    try:
        await db.flush()
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning(
            "category %s persist source_category backfill failed",
            cid,
            exc_info=True,
        )
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass

    # IDs only — provision_users_now calls expire_all(); holding ORM User
    # across that made category resync crash (MissingGreenlet) and abort the
    # whole wave so new groups never reached category-bound users.
    user_meta = {
        int(u.id): str(getattr(u, "username", "") or f"#{u.id}")
        for u in users_by_id.values()
    }
    user_ids = sorted(user_meta.keys())
    try:
        db.expunge_all()
    except Exception:  # noqa: BLE001
        pass

    if not user_ids:
        logger.info(
            "category %s resync — no bound users (desired=%s added=%s removed=%s)",
            cid,
            desired,
            added,
            force_removed,
        )
        if job_id:
            finish_job_id(
                job_id,
                ok=0,
                message="کاربری در این دسته برای اعمال روی ساب نبود",
            )
        return 0

    n_users = len(user_ids)
    update_job(
        job_id,
        total=n_users,
        done=0,
        message=f"{n_users} کاربر پیدا شد — شروع اعمال روی ساب…",
    )

    ok = 0
    failed = 0
    touched_gids: set[int] = set(desired) | set(force_removed) | set(added)
    touched_uids: list[int] = []
    # Never treat a remove-wave as "heal desired" — that skipped force_removed
    # deletes when membership already matched and left remotes on the sub.
    if added:
        heal_gids = list(added)
    elif force_removed:
        heal_gids = []
    else:
        heal_gids = list(desired)

    for idx, uid in enumerate(user_ids, start=1):
        uname = user_meta.get(uid, f"#{uid}")
        action = "اضافه/همگام پنل روی ساب"
        if force_removed and not desired:
            action = "حذف پنل از ساب و پنل اصلی"
        elif force_removed and not added:
            action = "حذف گروه قبلی و همگام‌سازی"
        update_job(
            job_id,
            message=f"{idx}/{n_users} · «{uname}» — {action}…",
        )
        try:
            # Clear poisoned session from a prior user failure / expire_all
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass

            result = await db.execute(
                select(UserModel)
                .where(UserModel.id == uid)
                .options(
                    selectinload(UserModel.panel_accounts).selectinload(
                        PanelAccount.panel
                    )
                )
            )
            user = result.scalars().unique().first()
            if user is None:
                bump_job(job_id, ok=True)
                continue

            # Keep category bind sticky while we sync (manual group edits may
            # have cleared it; discovery backfill alone is not enough).
            if getattr(user, "source_category_id", None) is None:
                user.source_category_id = cid

            changed = False
            # Always drive membership to exactly ``desired`` (including []).
            if await apply_user_group_membership(db, user, desired):
                changed = True
                # Finish leftovers after the longer membership provision wait.
                try:
                    await retry_failed_accounts(db, user)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "category %s retry after membership user=%s failed",
                        cid,
                        uname,
                        exc_info=True,
                    )
            elif heal_gids:
                # Membership already matches — provision heal targets fully
                # (longer wait + retry so main-panel remotes are not left pending).
                panels = await resolve_panels_for_groups(db, heal_gids)
                if panels:
                    stats = await provision_users_now(
                        db, [user], panels, wait=14.0
                    )
                    try:
                        await retry_failed_accounts(db, user)
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "category %s retry after heal user=%s failed",
                            cid,
                            uname,
                            exc_info=True,
                        )
                    pending = int((stats or {}).get("pending") or 0)
                    err_n = int((stats or {}).get("err") or 0)
                    changed = True
                    if pending or err_n:
                        logger.info(
                            "category %s heal user=%s pending=%s err=%s",
                            cid,
                            uname,
                            pending,
                            err_n,
                        )

            # Always cut removed-group remotes (even if membership already
            # matched desired). used_traffic stays on deleted PanelAccount rows.
            if force_removed:
                await sync_delete_user_group_accounts(
                    db, user, force_removed, fast=False
                )
                changed = True

            try:
                await db.commit()
            except Exception:  # noqa: BLE001
                try:
                    await db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                raise

            if changed:
                ok += 1
                touched_uids.append(uid)
            bump_job(job_id, ok=True)
        except Exception:  # noqa: BLE001
            failed += 1
            bump_job(job_id, ok=False)
            logger.exception(
                "category %s resync user_id=%s (%s) failed",
                cid,
                uid,
                uname,
            )
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass

    if touched_uids:
        invalidate_user_feed_cache(*touched_uids)

    # Local recount only — never live-pull remotes here (that refunds/inflates
    # volume after a config is cut).
    update_job(job_id, message="بازشماری مصرف گروه‌ها…")
    for gid in sorted(touched_gids):
        try:
            await recount_group_usage_local(db, gid)
        except Exception:  # noqa: BLE001
            logger.warning(
                "category %s recount group=%s failed", cid, gid, exc_info=True
            )
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
    processed = ok + failed
    logger.info(
        "category %s membership resync: changed=%s/%s failed=%s groups=%s added=%s",
        cid,
        ok,
        n_users,
        failed,
        desired,
        added,
    )
    done_msg = (
        f"تمام شد — {processed} از {n_users} کاربر بررسی شد"
        f" · همگام {ok}"
        + (f" · خطا {failed}" if failed else "")
    )
    if job_id:
        update_job(
            job_id,
            done=n_users,
            total=n_users,
            ok=ok,
            failed=failed,
            message=done_msg,
        )
        # Always finish here so dashboard counters are not overwritten by a
        # wrapper that only knew the «changed» return value.
        finish_job_id(job_id, ok=ok, failed=failed, message=done_msg)
    return ok


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
    3. If Astra quota is exhausted (limited/expired/disabled), freeze remotes
       so new configs stay cut like the rest of the sub.
    4. Recount weighted usage.

    Users run in a small parallel pool (separate DB sessions); provision uses
    ``fast=True`` and Astra Xray rebuild runs once per panel after the wave.

    If panels change while a run is active, the wave is cancelled and restarted
    against the latest GroupPanel rows (so a quick add→remove actually removes).
    """
    gid = int(group_id)
    lock = _group_resync_lock(gid)
    seed_removed = {
        int(p) for p in (removed_panel_ids or []) if p is not None
    }
    if lock.locked() or gid in _group_resync_running:
        mark_group_resync_dirty(gid, list(seed_removed))
        logger.info("group %s resync queued restart (already running)", gid)
        return 0

    followup = False
    total_ok = 0
    async with lock:
        _group_resync_running.add(gid)
        try:
            while True:
                _group_resync_cancel.discard(gid)
                removed = set(seed_removed)
                seed_removed = set()
                removed |= _group_resync_dirty_removed.pop(gid, set())
                try:
                    await db.commit()
                except Exception:  # noqa: BLE001
                    try:
                        await db.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    db.expire_all()
                except Exception:  # noqa: BLE001
                    pass

                n, _jid = await _resync_users_for_group_locked(
                    db,
                    gid,
                    removed_panel_ids=sorted(removed),
                )
                total_ok += int(n or 0)

                # Newer save arrived mid-run → apply again with fresh membership
                if gid in _group_resync_cancel or gid in _group_resync_dirty_removed:
                    logger.info(
                        "group %s resync restarting after mid-run panel change",
                        gid,
                    )
                    continue
                break
        except Exception:
            try:
                from app.services.ops_progress import finish_group_job

                finish_group_job(
                    gid, ok=0, failed=1, message="همگام‌سازی با خطا متوقف شد"
                )
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            # Save that raced after the last cancel-check still needs a scrub
            if gid in _group_resync_cancel or gid in _group_resync_dirty_removed:
                followup = True
            _group_resync_running.discard(gid)
            _group_resync_cancel.discard(gid)

    if followup:
        leftover = sorted(_group_resync_dirty_removed.get(gid, set()))
        logger.info(
            "group %s resync follow-up after race leftover=%s", gid, leftover
        )
        more = await resync_users_for_group(
            db, gid, removed_panel_ids=leftover
        )
        return total_ok + int(more or 0)
    return total_ok


async def _resync_users_for_group_locked(
    db: AsyncSession,
    gid: int,
    *,
    removed_panel_ids: list[int] | None = None,
) -> tuple[int, str | None]:
    from app.core.database import AsyncSessionLocal
    from app.models.group import Group
    from app.models.group_config import GroupConfigItem
    from app.services.group_configs import (
        _get_sample_user,
        sample_username_for_group,
        sync_items_from_sample,
    )
    from app.services.ops_progress import (
        bump_phase,
        finish_group_job,
        finish_phase,
        note_job_error,
        set_phase,
        start_group_resync,
    )
    from sqlalchemy import delete

    ops_job_id: str | None = None

    removed_hint = {int(p) for p in (removed_panel_ids or []) if p is not None}
    # Membership = all GroupPanel rows (even inactive panels keep remotes until
    # the admin actually removes the panel from the group).
    gp_ids = await db.execute(
        select(GroupPanel.panel_id).where(GroupPanel.group_id == gid)
    )
    desired_panel_ids = {int(pid) for (pid,) in gp_ids.all() if pid is not None}
    # Provision only active panels (same as create-user / heal).
    targets = await resolve_panels_for_groups(db, [gid])
    # Serializable specs so worker sessions can rebuild Panel objects safely.
    panel_specs: list[tuple[int, dict, int]] = [
        (int(p.id), dict(cfg or {}), int(g)) for p, cfg, g in targets
    ]
    astra_panel_ids = {
        int(p.id) for p, _, _ in targets if getattr(p, "type", None) == PanelType.astra
    }

    group_row = await db.get(Group, gid)
    group_name = str(getattr(group_row, "name", "") or "") if group_row else ""

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

    def _stale_for_user(
        accounts: list[PanelAccount],
        still_claimed: set[int],
    ) -> list[PanelAccount]:
        stale: list[PanelAccount] = []
        seen_acc: set[int] = set()
        for a in accounts:
            if a.status == PanelAccountStatus.deleted:
                continue
            aid = int(a.id)
            pid = int(a.panel_id)
            agid = getattr(a, "group_id", None)
            if agid is not None and int(agid) == gid and pid not in desired_panel_ids:
                stale.append(a)
                seen_acc.add(aid)
                continue
            if pid in removed_hint and pid not in still_claimed and aid not in seen_acc:
                stale.append(a)
                seen_acc.add(aid)
                continue
            if agid is None and pid not in still_claimed and aid not in seen_acc:
                stale.append(a)
                seen_acc.add(aid)
        return stale

    async def _user_claims(sdb: AsyncSession, user_id: int) -> set[int]:
        ug_all = await sdb.execute(
            select(UserGroup.group_id).where(UserGroup.user_id == user_id)
        )
        user_gids = [int(r[0]) for r in ug_all.all() if r[0] is not None]
        if not user_gids:
            return set()
        claim_q = await sdb.execute(
            select(GroupPanel.panel_id).where(GroupPanel.group_id.in_(user_gids))
        )
        return {int(pid) for (pid,) in claim_q.all() if pid is not None}

    async def _load_user_targets(
        sdb: AsyncSession,
    ) -> list[tuple[Panel, dict, int]]:
        if not panel_specs:
            return []
        pids = list({pid for pid, _, _ in panel_specs})
        rows = await sdb.execute(select(Panel).where(Panel.id.in_(pids or [-1])))
        by_id = {int(p.id): p for p in rows.scalars().all()}
        return [
            (by_id[pid], cfg, g)
            for pid, cfg, g in panel_specs
            if pid in by_id and bool(getattr(by_id[pid], "is_active", True))
        ]

    async def _phase_delete_user(sdb: AsyncSession, user: User) -> None:
        acc_q = await sdb.execute(
            select(PanelAccount)
            .where(PanelAccount.user_id == user.id)
            .options(selectinload(PanelAccount.panel))
        )
        accounts = list(acc_q.scalars().all())
        user.panel_accounts = accounts
        still_claimed = await _user_claims(sdb, int(user.id))
        stale = _stale_for_user(accounts, still_claimed)
        if not stale:
            return
        await _delete_panel_accounts_remote(
            sdb, user, stale, clear_links_always=True, fast=True
        )
        for a in stale:
            if int(a.panel_id) not in still_claimed:
                a.status = PanelAccountStatus.deleted
                a.links = []
                a.links_by_group = {}
        await sdb.flush()

    async def _phase_add_user(
        sdb: AsyncSession,
        user: User,
        user_targets: list[tuple[Panel, dict, int]],
        *,
        skip_rebuild: bool,
    ) -> None:
        if user_targets:
            await provision_user_on_panels(
                sdb,
                user,
                user_targets,
                fast=True,
                skip_astra_rebuild=skip_rebuild,
            )
        if user.status in (
            UserStatus.limited,
            UserStatus.expired,
            UserStatus.disabled,
        ):
            try:
                await sync_modify_user(
                    sdb,
                    user,
                    skip_nginx_sync=True,
                    freeze_fast=True,
                    remote_timeout=8,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "group %s freeze user=%s failed",
                    gid,
                    user.username,
                    exc_info=True,
                )

    async def _phase_retry_user(sdb: AsyncSession, user: User) -> None:
        await retry_failed_accounts(sdb, user)

    # Sample probe first — keeps Configs page + hide rules panel-aligned
    sample_name = sample_username_for_group(gid)
    sample = await _get_sample_user(db, gid)
    if sample is not None:
        try:
            await _phase_delete_user(db, sample)
            await _phase_add_user(db, sample, targets, skip_rebuild=True)
            sample = await _get_sample_user(db, gid)
            group = await db.get(Group, gid)
            if sample is not None and group is not None:
                await sync_items_from_sample(db, group, sample)
            await db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("group %s sample reconcile failed", gid)
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass

    ug = await db.execute(select(UserGroup.user_id).where(UserGroup.group_id == gid))
    user_ids = [int(row[0]) for row in ug.all() if row[0] is not None]

    # Category-bound buyers must be on the work list even if membership
    # insert lagged (or category resync was skipped). Align each missing
    # user to FULL category groups (not a lone UserGroup at sort 900) so
    # their sub matches every group of the sales category.
    cat_id = getattr(group_row, "category_id", None) if group_row else None
    if cat_id is not None:
        crow = await db.execute(
            select(User.id).where(User.source_category_id == int(cat_id))
        )
        cat_uids = [int(r[0]) for r in crow.all() if r[0] is not None]
        have = set(user_ids)
        missing = [uid for uid in cat_uids if uid not in have]
        if missing:
            desired_cat = await category_group_ids(db, int(cat_id))
            for uid in missing:
                try:
                    urow = await db.execute(
                        select(User)
                        .where(User.id == int(uid))
                        .options(
                            selectinload(User.panel_accounts).selectinload(
                                PanelAccount.panel
                            )
                        )
                    )
                    u = urow.scalars().unique().first()
                    if u is None:
                        continue
                    await apply_user_group_membership(db, u, desired_cat)
                    user_ids.append(int(uid))
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "group %s bind category user=%s failed",
                        gid,
                        uid,
                        exc_info=True,
                    )
                    try:
                        await db.rollback()
                    except Exception:  # noqa: BLE001
                        pass
            try:
                await db.flush()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "group %s bind category users flush failed n=%s",
                    gid,
                    len(missing),
                    exc_info=True,
                )
                try:
                    await db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                ug2 = await db.execute(
                    select(UserGroup.user_id).where(UserGroup.group_id == gid)
                )
                user_ids = [int(row[0]) for row in ug2.all() if row[0] is not None]

    if not user_ids:
        await db.flush()
        ops_job_id = start_group_resync(
            group_id=gid, group_name=group_name, users_total=0
        )
        finish_group_job(
            gid,
            ok=0,
            failed=0,
            message=(
                "کاربری برای اعمال نبود"
                + (
                    " — اول همگام دسته را بزنید یا source_category کاربران را چک کنید"
                    if cat_id is not None
                    else ""
                )
            ),
        )
        return 0, ops_job_id

    # Stable order — newest first so recent buyers get slots sooner
    user_ids = sorted(set(user_ids), reverse=True)
    # Drop sample-probe accounts from the work list (and from progress totals)
    sample_skip: set[int] = set()
    if user_ids:
        probe_rows = await db.execute(
            select(User.id, User.username, User.note).where(User.id.in_(user_ids))
        )
        for uid, uname, note in probe_rows.all():
            name = str(uname or "")
            if (
                name == sample_name
                or name.startswith(f"ag{gid}s")
                or "نمونه کانفیگ" in (note or "")
            ):
                sample_skip.add(int(uid))
    work_ids = [uid for uid in user_ids if uid not in sample_skip]
    if not work_ids:
        ops_job_id = start_group_resync(
            group_id=gid, group_name=group_name, users_total=0
        )
        finish_group_job(gid, ok=0, failed=0, message="کاربری برای اعمال نبود")
        return 0, ops_job_id

    ops_job_id = start_group_resync(
        group_id=gid,
        group_name=group_name,
        users_total=len(work_ids),
    )

    ok = 0
    failed = 0
    touched_uids: list[int] = []
    user_conc = min(8, max(2, len(work_ids)))
    user_sem = asyncio.Semaphore(user_conc)
    ok_lock = asyncio.Lock()

    def _skip_user(user: User) -> bool:
        uname = str(user.username or "")
        if uname == sample_name or uname.startswith(f"ag{gid}s"):
            return True
        if "نمونه کانفیگ" in (user.note or ""):
            return True
        return False

    async def _run_phase(
        phase_id: str,
        *,
        label_msg: str,
        handler,  # async (sdb, user) -> None
    ) -> tuple[int, int, bool]:
        """Returns (ok, fail, cancelled)."""
        phase_ok = 0
        phase_fail = 0
        cancelled = False
        set_phase(
            ops_job_id or "",
            phase_id,
            status="running",
            total=len(work_ids),
            message=label_msg,
        )

        def _transient_db_err(exc: BaseException) -> bool:
            msg = str(exc or "").lower()
            return any(
                needle in msg
                for needle in (
                    "connection is closed",
                    "connectiondoesnotexist",
                    "pendingrollback",
                    "server closed the connection",
                    "connection was closed",
                    "cannot call transaction",
                )
            )

        async def _run_one(uid: int) -> str | None:
            """Apply phase for one user. Returns username on success, else raises."""
            async with AsyncSessionLocal() as sdb:
                result = await sdb.execute(
                    select(User)
                    .where(User.id == uid)
                    .options(
                        selectinload(User.panel_accounts).selectinload(
                            PanelAccount.panel
                        )
                    )
                )
                user = result.scalars().unique().first()
                if user is None or _skip_user(user):
                    return None
                uname = str(user.username or uid)
                await handler(sdb, user)
                try:
                    await sdb.commit()
                except Exception:  # noqa: BLE001
                    try:
                        await sdb.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                    raise
                return uname

        async def _worker(uid: int) -> None:
            nonlocal phase_ok, phase_fail
            if group_resync_should_cancel(gid):
                return
            async with user_sem:
                if group_resync_should_cancel(gid):
                    return
                last_exc: BaseException | None = None
                for attempt in range(2):
                    try:
                        uname = await _run_one(uid)
                        bump_phase(ops_job_id or "", phase_id, ok=True)
                        async with ok_lock:
                            phase_ok += 1
                            if uname is not None and uid not in touched_uids:
                                touched_uids.append(int(uid))
                        return
                    except Exception as exc:  # noqa: BLE001
                        last_exc = exc
                        if attempt == 0 and _transient_db_err(exc):
                            logger.warning(
                                "group %s phase=%s user_id=%s transient db — retry",
                                gid,
                                phase_id,
                                uid,
                            )
                            await asyncio.sleep(0.35)
                            continue
                        break
                bump_phase(ops_job_id or "", phase_id, ok=False)
                async with ok_lock:
                    phase_fail += 1
                brief = _exc_msg(last_exc) if last_exc is not None else "خطای ناشناخته"
                if _transient_db_err(last_exc or Exception("")):
                    brief = "قطع اتصال دیتابیس هنگام اعمال — دوباره تلاش کنید"
                note_job_error(
                    ops_job_id or "",
                    f"کاربر #{uid} · فاز {phase_id}: {brief}",
                )
                logger.exception(
                    "group %s phase=%s user_id=%s failed",
                    gid,
                    phase_id,
                    uid,
                )

        wave = max(user_conc * 4, 12)
        for start in range(0, len(work_ids), wave):
            if group_resync_should_cancel(gid):
                cancelled = True
                break
            chunk = work_ids[start : start + wave]
            await asyncio.gather(*[_worker(uid) for uid in chunk])
            await asyncio.sleep(0.02)
            if group_resync_should_cancel(gid):
                cancelled = True
                break

        finish_phase(
            ops_job_id or "",
            phase_id,
            status="done" if not cancelled else "error",
            message=(
                None
                if not cancelled
                else "قطع شد — اعمال مجدد با آخرین پنل‌های گروه…"
            ),
        )
        return phase_ok, phase_fail, cancelled

    async def _add_handler(sdb: AsyncSession, user: User) -> None:
        await _phase_add_user(
            sdb,
            user,
            await _load_user_targets(sdb),
            skip_rebuild=True,
        )

    t_wave = time.monotonic()
    aborted = False

    # 1) حذف
    d_ok, d_fail, d_cancel = await _run_phase(
        "delete",
        label_msg="در حال حذف پنل‌های جدا‌شده از ساب و پنل اصلی…",
        handler=_phase_delete_user,
    )
    if d_cancel:
        aborted = True
        a_ok = a_fail = r_ok = r_fail = 0
    else:
        # 2) اضافه
        a_ok, a_fail, a_cancel = await _run_phase(
            "add",
            label_msg="در حال اضافه و همگام‌سازی پنل‌های گروه روی ساب…",
            handler=_add_handler,
        )
        if a_cancel:
            aborted = True
            r_ok = r_fail = 0
        else:
            # 3) تلاش خطاها (فقط اکانت‌های error/pending همین کاربران)
            r_ok, r_fail, r_cancel = await _run_phase(
                "retry",
                label_msg="در حال تلاش مجدد برای خطاهای باقی‌مانده…",
                handler=_phase_retry_user,
            )
            if r_cancel:
                aborted = True

    ok = max(d_ok, a_ok, r_ok)
    failed = d_fail + a_fail + r_fail

    if aborted:
        finish_group_job(
            gid,
            ok=ok,
            failed=failed,
            message="قطع شد — الان با آخرین تغییرات گروه از نو اعمال می‌شود…",
        )
        logger.info(
            "group %s resync aborted mid-run for newer panel change ok=%s",
            gid,
            ok,
        )
        return ok, ops_job_id

    logger.info(
        "group %s phases done delete=%s/%s add=%s/%s retry=%s/%s elapsed=%.1fs",
        gid,
        d_ok,
        d_fail,
        a_ok,
        a_fail,
        r_ok,
        r_fail,
        time.monotonic() - t_wave,
    )

    # One Astra rebuild per panel for the whole resync (not per user)
    if astra_panel_ids and ok > 0:
        try:
            from app.services import astra_node as astra_node_svc

            async with AsyncSessionLocal() as sdb:
                for pid in sorted(astra_panel_ids):
                    panel_row = await sdb.get(Panel, int(pid))
                    if panel_row is None:
                        continue
                    try:
                        await astra_node_svc.rebuild_xray_config(
                            sdb, panel_row, sync_nginx=False
                        )
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "group %s post-resync astra rebuild panel=%s failed",
                            gid,
                            pid,
                            exc_info=True,
                        )
                try:
                    await astra_node_svc.sync_nginx_ws_upstreams(sdb)
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "group %s post-resync nginx sync failed", gid, exc_info=True
                    )
                await sdb.commit()
        except Exception:  # noqa: BLE001
            logger.warning(
                "group %s post-resync astra rebuild failed", gid, exc_info=True
            )

    try:
        await recount_group_usage_local(db, gid)
    except Exception:  # noqa: BLE001
        logger.warning("group %s recount after resync failed", gid, exc_info=True)

    if touched_uids:
        try:
            from app.api.subscription import invalidate_user_feed_cache

            invalidate_user_feed_cache(*touched_uids)
        except Exception:  # noqa: BLE001
            pass

    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("group %s final resync commit failed", gid, exc_info=True)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass

    # Coverage snapshot for dashboard — how many remotes still pending/error
    pending_acc = 0
    error_acc = 0
    active_acc = 0
    try:
        if work_ids and desired_panel_ids:
            cov = await db.execute(
                select(PanelAccount.status, func.count())
                .where(
                    PanelAccount.user_id.in_(work_ids),
                    PanelAccount.group_id == gid,
                    PanelAccount.status.in_(
                        (
                            PanelAccountStatus.active,
                            PanelAccountStatus.pending,
                            PanelAccountStatus.error,
                        )
                    ),
                )
                .group_by(PanelAccount.status)
            )
            for st, n in cov.all():
                nn = int(n or 0)
                if st == PanelAccountStatus.active:
                    active_acc = nn
                elif st == PanelAccountStatus.pending:
                    pending_acc = nn
                elif st == PanelAccountStatus.error:
                    error_acc = nn
    except Exception:  # noqa: BLE001
        logger.debug("group %s coverage snapshot failed", gid, exc_info=True)

    finish_msg = (
        f"تمام شد — {ok} از {len(work_ids)} کاربر"
        + (f" · خطای موج: {failed}" if failed else "")
        + f" · اکانت فعال: {active_acc}"
        + (f" · در انتظار: {pending_acc}" if pending_acc else "")
        + (f" · خطای پنل: {error_acc}" if error_acc else "")
    )
    finish_group_job(
        gid,
        ok=ok,
        failed=failed + error_acc,
        message=finish_msg,
    )

    logger.info(
        "group %s panel resync done: ok=%s failed=%s total=%s desired_panels=%s removed=%s "
        "active_acc=%s pending_acc=%s error_acc=%s",
        gid,
        ok,
        failed,
        len(work_ids),
        sorted(desired_panel_ids),
        sorted(removed_hint),
        active_acc,
        pending_acc,
        error_acc,
    )
    return ok, ops_job_id


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
