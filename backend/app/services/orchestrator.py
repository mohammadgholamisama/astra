import asyncio
import logging
import re
import secrets
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.adapters.base import AdapterResult, UserPayload
from app.adapters.factory import get_adapter
from app.core.config import get_settings
from app.models.group import GroupPanel, UserGroup
from app.models.panel import Panel
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
    return UserPayload(
        username=uname,
        data_limit=data_limit,
        expire=user.expire or None,
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
) -> AdapterResult:
    """Create on remote; if username taken/unreadable, retry with `{name}_p{panel}` aliases.

    Ensures Astra never stuck on 409 from another admin's reserved name.
    """
    preferred = sanitize_remote_username(payload.username or astra_username)
    payload.username = preferred
    payload.note = payload.note  # already tagged in build_user_payload

    last = _fail_result("create failed")
    # attempt -1 = preferred name; 0..3 = aliases
    for attempt in range(-1, 4):
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
        except Exception as e:  # noqa: BLE001
            last = _fail_result(_exc_msg(e))
            # Timeout: confirm preferred/alias might already exist
            try:
                usage = await asyncio.wait_for(
                    adapter.get_usage(payload.username, expand_links=False),  # type: ignore[call-arg]
                    timeout=8,
                )
                if usage and usage.success:
                    return usage
            except TypeError:
                try:
                    usage = await asyncio.wait_for(
                        adapter.get_usage(payload.username), timeout=8
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
        if attempt < 0:
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
    """Merge two GroupPanel configs for the same remote panel.

    Critical when admin selects e.g. Hajmi4-Direct + Hajmi4-Tunnel:
    - protocols / inbounds are unioned (not overwritten)
    - PasarGuard remote_group_ids / group_ids are unioned
    - coefficient = max
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


async def resolve_panels_for_groups(db: AsyncSession, group_ids: list[int]) -> list[tuple[Panel, dict]]:
    """Return unique (panel, merged_config) for all panels in given groups.

    Same panel in multiple groups → one account; configs unioned (protocols/inbounds/groups).
    """
    if not group_ids:
        return []
    result = await db.execute(
        select(GroupPanel)
        .where(GroupPanel.group_id.in_(group_ids))
        .options(selectinload(GroupPanel.panel))
        .order_by(GroupPanel.priority.desc())
    )
    links = result.scalars().all()
    seen: dict[int, tuple[Panel, dict]] = {}
    for link in links:
        panel = link.panel
        if not panel or not panel.is_active:
            continue
        cfg = dict(link.config or {})
        if panel.id not in seen:
            seen[panel.id] = (panel, cfg)
            continue
        prev_cfg = dict(seen[panel.id][1])
        seen[panel.id] = (panel, merge_group_panel_configs(prev_cfg, cfg))
    return list(seen.values())


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


async def provision_user_on_panels(
    db: AsyncSession,
    user: User,
    panels: list[tuple[Panel, dict]],
) -> list[PanelAccount]:
    """Create on all remotes in parallel (capped concurrency).

    Wall time ~ ceil(N / PANEL_REMOTE_CONCURRENCY) * slowest panel.
    """
    if not panels:
        return []

    t_prov = time.monotonic()
    panel_ids = [p.id for p, _ in panels]
    existing_rows = await db.execute(
        select(PanelAccount).where(
            PanelAccount.user_id == user.id,
            PanelAccount.panel_id.in_(panel_ids),
        )
    )
    by_panel = {a.panel_id: a for a in existing_rows.scalars().all()}

    jobs: list[tuple[PanelAccount, Panel, UserPayload]] = []
    created_any = False
    for panel, config in panels:
        account = by_panel.get(panel.id)
        if account is None:
            account = PanelAccount(
                user_id=user.id, panel_id=panel.id, status=PanelAccountStatus.pending
            )
            db.add(account)
            by_panel[panel.id] = account
            created_any = True
        # Prefer stored alias if this panel already collided before
        preferred = (account.remote_username or user.username or "").strip()
        payload = build_user_payload(
            user,
            uuid=account.uuid,
            panel_config=config,
            remote_username=preferred,
        )
        jobs.append((account, panel, payload))

    if created_any:
        await db.flush()

    sem = _remote_sem()

    async def _remote(panel: Panel, payload: UserPayload) -> AdapterResult:
        adapter = get_adapter(panel)
        t0 = time.monotonic()
        create_timeout = 30.0
        ptype = getattr(panel.type, "value", panel.type)
        if ptype in ("marzban", "pasarguard", "rebecca"):
            create_timeout = 35.0
        try:
            async with sem:
                return await create_user_with_collision_alias(
                    adapter,
                    payload,
                    astra_username=user.username,
                    panel_id=panel.id,
                    timeout=create_timeout,
                )
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

    results = await asyncio.gather(
        *[_remote(panel, payload) for _, panel, payload in jobs]
    )

    accounts: list[PanelAccount] = []
    now = datetime.now(timezone.utc)
    for (account, panel, payload), result in zip(jobs, results):
        coeff = coefficient_from_config(payload.config or {})
        if result.success:
            account.status = PanelAccountStatus.active
            # Persist the name actually used on remote (may be alias)
            account.remote_username = (
                result.remote_username or payload.username or user.username
            )
            account.remote_id = result.remote_id or account.remote_id
            account.uuid = result.uuid or account.uuid
            account.links = result.links or account.links or []
            account.used_traffic = int(result.used_traffic or 0)
            account.coeff_anchor_raw = 0
            account.coeff_anchor_weighted = 0
            account.applied_coefficient = coeff
            account.error_message = None
            account.last_sync_at = now
            panel.last_ok_at = now
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
    # Expand /sub refs now so first /sub page has ALL panel configs (esp. PasarGuard)
    try:
        reloaded = await db.execute(
            select(User)
            .where(User.id == user.id)
            .options(selectinload(User.panel_accounts).selectinload(PanelAccount.panel))
        )
        fresh = reloaded.scalar_one_or_none() or user
        await asyncio.wait_for(hydrate_share_links(db, fresh), timeout=14.0)
        await db.flush()
    except Exception as exc:  # noqa: BLE001
        logger.warning("provision hydrate user=%s: %s", user.username, _exc_msg(exc))

    logger.info(
        "provision done user=%s panels=%s ok=%s total=%.2fs",
        user.username,
        len(jobs),
        sum(1 for a in accounts if a.status == PanelAccountStatus.active),
        time.monotonic() - t_prov,
    )
    return accounts



async def reset_user_traffic_on_panels(db: AsyncSession, user: User) -> list[dict]:
    """Reset used traffic on every remote panel, then clear local counters.

    Returns per-panel {panel_id, panel_name, ok, error}.
    """
    result = await db.execute(
        select(PanelAccount)
        .where(PanelAccount.user_id == user.id)
        .options(selectinload(PanelAccount.panel))
    )
    accounts = list(result.scalars().all())

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
        adapter = get_adapter(panel)
        uname = (account.remote_username or user.username or "").strip()
        rid = account.remote_id or account.remote_username or uname
        try:
            res = await asyncio.wait_for(
                adapter.reset_usage(uname, rid), timeout=18
            )
        except Exception as exc:  # noqa: BLE001
            res = _fail_result(_exc_msg(exc))
        if res.success:
            account.used_traffic = int(res.used_traffic or 0)
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

    outcomes = await asyncio.gather(*[_one(a) for a in accounts]) if accounts else []
    user.lifetime_used_traffic = int(user.lifetime_used_traffic or 0) + int(
        user.used_traffic or 0
    )
    user.used_traffic = 0
    if user.status == UserStatus.limited:
        user.status = UserStatus.active
    await db.flush()
    # Re-push fresh shared caps / active status
    await sync_modify_user(db, user)
    return list(outcomes)


async def sync_modify_user(db: AsyncSession, user: User) -> None:
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
    panel_cfg: dict[int, dict] = {}
    if group_ids:
        for panel, cfg in await resolve_panels_for_groups(db, group_ids):
            panel_cfg[panel.id] = cfg

    coeffs = await panel_coefficients_for_user(db, user)
    weighted = await _weighted_total(accounts, coeffs)
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
        # Skip orphan panel rows (no longer in user's groups) unless freezing all
        if panel.id not in panel_cfg and not blocked:
            # Drop noisy false errors from leftover accounts of removed groups
            if account.status == PanelAccountStatus.error and not (account.links or []):
                account.status = PanelAccountStatus.deleted
                account.error_message = None
            continue
        # Still push disable/limits to error accounts — they often exist on remote
        merged = dict(panel_cfg.get(panel.id) or {})
        rem_name = (account.remote_username or user.username or "").strip()
        payload = build_user_payload(
            user,
            uuid=account.uuid,
            panel_config=merged,
            used_weighted_total=weighted,
            used_raw_on_panel=int(account.used_traffic or 0),
            remote_username=rem_name,
        )
        jobs.append((account, panel, payload))

    sem = _remote_sem()

    async def _remote(
        panel: Panel, payload: UserPayload, account: PanelAccount
    ) -> AdapterResult:
        attempts = 2 if blocked else 1
        last = _fail_result("modify failed")
        for i in range(attempts):
            adapter = get_adapter(panel)
            try:
                async with sem:
                    last = await asyncio.wait_for(
                        adapter.modify_user(payload), timeout=25
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
                        timeout=30.0,
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

    if not jobs:
        await db.flush()
        return

    results = await asyncio.gather(
        *[_remote(panel, payload, account) for account, panel, payload in jobs]
    )
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
            # Only update raw usage when panel returned a real reading — never wipe
            if res.used_traffic is not None and int(res.used_traffic) >= 0:
                # Prefer max so a create-on-missing (0) does not erase prior raw
                prev = int(account.used_traffic or 0)
                got = int(res.used_traffic or 0)
                account.used_traffic = max(prev, got) if prev > 0 and got == 0 else got
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
            if was_ok and not _is_not_found_err(res.error):
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
            if blocked:
                logger.warning(
                    "failed to freeze remote user=%s panel=%s: %s",
                    user.username,
                    account.panel_id,
                    account.error_message or msg,
                )
    await db.flush()
    # Recompute weighted usage after any raw changes (existing panels unchanged)
    coeffs = await panel_coefficients_for_user(db, user)
    user.used_traffic = await _weighted_total(accounts, coeffs)
    await db.flush()


async def sync_delete_user(db: AsyncSession, user: User) -> list[dict]:
    """
    Delete user on every remote panel in parallel (capped concurrency).

    ok=True  → remote confirmed gone (DELETE ok or already 404)
    ok=False → remote still has the user / unreachable — caller must NOT
               delete the Astra user.

    Does not mark accounts deleted on failure (so retry can try again).
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

    async def _remote(account: PanelAccount) -> dict:
        panel = account.panel
        name = panel.name if panel else f"#{account.panel_id}"
        uname = (account.remote_username or user.username or "").strip()
        base = {
            "panel_id": account.panel_id,
            "panel_name": name,
            "account_id": account.id,
        }

        if not panel:
            return {**base, "ok": False, "error": "پنل پیدا نشد — حذف لغو شد"}
        if not uname:
            return {**base, "ok": False, "error": "یوزرنیم ریموت خالی است"}

        adapter = get_adapter(panel)
        try:
            async with sem:
                res = await asyncio.wait_for(
                    adapter.delete_user(
                        uname, account.remote_id or account.remote_username
                    ),
                    timeout=14,
                )
        except Exception as e:  # noqa: BLE001
            return {**base, "ok": False, "error": _exc_msg(e)}
        finally:
            try:
                await adapter.close()  # type: ignore[attr-defined]
            except Exception:
                pass

        if res.success:
            return {**base, "ok": True, "error": None}
        return {
            **base,
            "ok": False,
            "error": res.error or "حذف از پنل ناموفق بود",
        }

    outcomes = list(await asyncio.gather(*[_remote(a) for a in accounts]))

    # Apply ORM updates after gather (safe for AsyncSession)
    by_id = {a.id: a for a in accounts}
    for row in outcomes:
        account = by_id.get(row.get("account_id"))
        if account is None:
            continue
        if row.get("ok"):
            account.status = PanelAccountStatus.deleted
            account.error_message = None
        else:
            account.error_message = row.get("error") or "حذف از پنل ناموفق بود"

    await db.flush()
    logger.info(
        "delete done user=%s panels=%s ok=%s total=%.2fs",
        user.username,
        len(accounts),
        sum(1 for r in outcomes if r.get("ok")),
        time.monotonic() - t0,
    )
    return outcomes



async def retry_failed_accounts(db: AsyncSession, user: User) -> list[PanelAccount]:
    result = await db.execute(
        select(PanelAccount)
        .where(
            PanelAccount.user_id == user.id,
            PanelAccount.status == PanelAccountStatus.error,
        )
        .options(selectinload(PanelAccount.panel))
    )
    failed = result.scalars().all()
    if not failed:
        return []

    ug = await db.execute(select(UserGroup.group_id).where(UserGroup.user_id == user.id))
    group_ids = [row[0] for row in ug.all()]
    cfg_by_panel: dict[int, dict] = {}
    if group_ids:
        for panel, cfg in await resolve_panels_for_groups(db, group_ids):
            cfg_by_panel[panel.id] = cfg

    panels = [
        (a.panel, dict(cfg_by_panel.get(a.panel_id) or {}))
        for a in failed
        if a.panel
    ]
    return await provision_user_on_panels(db, user, panels)


async def collect_links(user: User, *, include_notices: bool = False) -> list[str]:
    return [
        row["link"]
        for row in collect_links_detailed(user, include_notices=include_notices)
        if row.get("link")
    ]


def collect_links_detailed(user: User, *, include_notices: bool = True) -> list[dict]:
    """All unique share links across panels.

    Order (every sub — client feed + HTML):
      1) status row (✨ user - remain - days ✨) — always, live values
      2) promo row — if enabled in settings
      3) group notices (کانفیگ اضافه) — optional, unchanged text
      4) real configs — ``#N. 2.5G | name`` or ``#N. text 2.5G | name``

    Only protocol share links (vless:// …) are returned — never raw /sub/ paths.
    """
    from app.adapters.http_utils import is_share_link
    from app.services.link_remark import (
        apply_display_name,
        build_promo_row,
        build_status_row,
        notice_rows_from_groups,
    )
    from app.services.site_settings import (
        get_config_brand_tag,
        get_promo_config_enabled,
        get_promo_config_text,
    )

    used = int(user.used_traffic or 0)
    limit = int(user.data_limit or 0)
    show_name = (getattr(user, "display_name", None) or "").strip() or (
        getattr(user, "username", None) or "user"
    )

    head: list[dict] = [
        build_status_row(
            username=show_name,
            used=used,
            limit=limit,
            expire=getattr(user, "expire", None),
        )
    ]
    if get_promo_config_enabled():
        promo = build_promo_row(get_promo_config_text())
        if promo:
            head.append(promo)

    notices = (
        notice_rows_from_groups(list(user.__dict__.get("groups") or []))
        if include_notices
        else []
    )
    rows: list[dict] = []
    seen: set[str] = set()
    for account in user.panel_accounts or []:
        # error accounts often still have working share links (sync flag only)
        if account.status not in (PanelAccountStatus.active, PanelAccountStatus.error):
            continue
        panel = account.__dict__.get("panel")
        panel_name = getattr(panel, "name", None) if panel is not None else None
        label = panel_name or f"پنل #{account.panel_id}"
        for link in account.links or []:
            text = (link or "").strip()
            if not text or text in seen or not is_share_link(text):
                continue
            seen.add(text)
            rows.append({"link": text, "panel": label, "panel_id": account.panel_id})
    renamed = apply_display_name(
        rows,
        getattr(user, "display_name", None),
        used=used,
        limit=limit,
        username=getattr(user, "username", None),
        brand_tag=get_config_brand_tag(),
    )
    # apply_display_name returns specials+real; we pass only reals so result is reals only
    return head + notices + renamed


async def hydrate_share_links(db: AsyncSession, user: User) -> int:
    """Parallel fetch of share links for every panel account missing them.

    Wave 1 — expand stored /sub URLs concurrently (no admin auth, ~6s).
    Wave 2 — get_usage + expand for remaining empties concurrently (~8s).
    Slow panels are cancelled; fast panels' results are kept.
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
        if not account.panel:
            return False
        links = [str(x).strip() for x in (account.links or []) if x and str(x).strip()]
        # Still hydrate when /sub (or https sub URL) remains — panel may have
        # returned only notices/partial share links alongside subscription_url.
        if any(is_subscription_ref(x) for x in links):
            return True
        if any(is_share_link(x) for x in links):
            if account.status == PanelAccountStatus.error:
                account.status = PanelAccountStatus.active
            return False
        # empty links → fetch from remote
        return True

    targets = [a for a in (user.panel_accounts or []) if _needs_hydrate(a)]
    if not targets:
        await db.flush()
        return 0

    updated = 0
    now = datetime.now(timezone.utc)

    async def _expand_stored(account: PanelAccount) -> bool:
        stored = [str(x).strip() for x in (account.links or []) if x and str(x).strip()]
        if not any(is_subscription_ref(x) for x in stored):
            return False
        panel = account.panel
        client = make_client(
            panel.base_url,
            bool((panel.meta or {}).get("verify_ssl", False)),
            timeout=8.0,
            xhr=False,
        )
        before_shares = sum(1 for x in stored if is_share_link(x))
        try:
            expanded = await asyncio.wait_for(
                expand_subscription_links(client, stored), timeout=10
            )
        except Exception:
            expanded = []
        finally:
            await client.aclose()
        shares = [x for x in (expanded or []) if is_share_link(x)]
        if not shares:
            return False
        # Prefer expanded set when /sub yielded more real configs
        if len(shares) < before_shares:
            return False
        account.links = shares
        account.status = PanelAccountStatus.active
        account.error_message = None
        account.last_sync_at = now
        return True

    async def _fetch_remote(account: PanelAccount) -> bool:
        panel = account.panel
        adapter = get_adapter(panel)
        uname = (account.remote_username or user.username or "").strip()
        rid = account.remote_id or account.remote_username or uname or None
        try:
            try:
                coro = adapter.get_usage(uname, rid, expand_links=False)  # type: ignore[call-arg]
            except TypeError:
                coro = adapter.get_usage(uname, rid)
            res = await asyncio.wait_for(coro, timeout=7)
            if not res or not res.success:
                return False

            raw_links = [str(x).strip() for x in (res.links or []) if x and str(x).strip()]
            shares = [x for x in raw_links if is_share_link(x)]
            if not shares and any(is_subscription_ref(x) for x in raw_links):
                client2 = make_client(
                    panel.base_url,
                    bool((panel.meta or {}).get("verify_ssl", False)),
                    timeout=6.0,
                    xhr=False,
                )
                try:
                    shares = await asyncio.wait_for(
                        expand_subscription_links(client2, raw_links), timeout=6
                    )
                except Exception:
                    shares = []
                finally:
                    await client2.aclose()

            if shares:
                account.links = shares
            elif raw_links:
                # Keep /sub refs for a later expand pass
                account.links = raw_links
            else:
                return False

            account.status = PanelAccountStatus.active
            account.error_message = None
            account.remote_username = (
                res.remote_username or account.remote_username or uname
            )
            account.remote_id = res.remote_id or account.remote_id
            if res.uuid:
                account.uuid = res.uuid
            account.used_traffic = int(res.used_traffic or account.used_traffic or 0)
            account.last_sync_at = datetime.now(timezone.utc)
            if panel:
                panel.last_ok_at = account.last_sync_at
            return bool(shares)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hydrate links failed user=%s panel=%s: %s",
                user.username,
                account.panel_id,
                _exc_msg(exc),
            )
            return False
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    pass

    async def _run_wave(coros: list, timeout: float) -> int:
        if not coros:
            return 0
        tasks = [asyncio.create_task(c) for c in coros]
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        ok = 0
        for t in done:
            try:
                if t.result():
                    ok += 1
            except Exception:
                pass
        return ok

    # Wave 1: expand any stored subscription URLs (all panels at once)
    need_expand = [
        a
        for a in targets
        if any(
            is_subscription_ref(str(x))
            for x in (a.links or [])
            if x and str(x).strip()
        )
    ]
    updated += await _run_wave([_expand_stored(a) for a in need_expand], timeout=12.0)

    # Wave 2: remaining accounts still missing share links
    remaining = [a for a in targets if _needs_hydrate(a)]
    updated += await _run_wave([_fetch_remote(a) for a in remaining], timeout=12.0)

    if updated:
        await db.flush()
    logger.info(
        "hydrate user=%s targets=%s updated=%s remaining=%s",
        user.username,
        len(targets),
        updated,
        len([a for a in targets if _needs_hydrate(a)]),
    )
    return updated


async def usage_breakdown_for_user(db: AsyncSession, user: User) -> list[dict]:
    """Per-panel raw usage with live coefficient → weighted (admin / internal)."""
    coeffs = await panel_coefficients_for_user(db, user)
    accounts = list(user.panel_accounts or [])
    _sync_account_coeff_periods(accounts, coeffs)
    user_limit = int(user.data_limit or 0)
    weighted_total = await _weighted_total(accounts, coeffs)
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
        coeff = _norm_coeff(coeffs.get(account.panel_id, 1.0))
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
    """Map panel_id -> coefficient from the user's groups (GroupPanel.config)."""
    ug = await db.execute(select(UserGroup.group_id).where(UserGroup.user_id == user.id))
    group_ids = [row[0] for row in ug.all()]
    if not group_ids:
        return {}
    result = await db.execute(select(GroupPanel).where(GroupPanel.group_id.in_(group_ids)))
    coeffs: dict[int, float] = {}
    for link in result.scalars().all():
        raw = (link.config or {}).get("coefficient", 1)
        try:
            c = float(raw if raw is not None else 1)
        except (TypeError, ValueError):
            c = 1.0
        if c < 0.01:
            c = 0.01
        # Multiple groups sharing a panel → take the higher coefficient
        prev = coeffs.get(link.panel_id)
        coeffs[link.panel_id] = max(prev, c) if prev is not None else c
    return coeffs


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
    accounts: list[PanelAccount], coeffs: dict[int, float]
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
        coeff = _norm_coeff(coeffs.get(account.panel_id, 1.0))
        if apply_coefficient_period(account, coeff):
            changed = True
    return changed


async def _weighted_total(
    accounts: list[PanelAccount], coeffs: dict[int, float]
) -> int:
    _sync_account_coeff_periods(accounts, coeffs)
    total = 0
    for account in accounts:
        if account.status not in (
            PanelAccountStatus.active,
            PanelAccountStatus.error,
        ):
            continue
        coeff = _norm_coeff(coeffs.get(account.panel_id, 1.0))
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
    coeffs = await panel_coefficients_for_user(db, user)
    return quota_math.needs_remote_cap_sync(
        status_changed=status_changed,
        blocked=blocked,
        force=force,
        active_panel_count=len(active),
        coefficients=[coeffs.get(a.panel_id, 1.0) for a in active],
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
) -> None:
    """Live-pull usage for many users. Push remotes when shared caps / disable needed.

    quick=True → short timeouts / single try (admin list background sync).
    """
    if not users:
        return
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(u: User) -> None:
        async with sem:
            try:
                await refresh_usage(db, u, force=force, quick=quick)
                changed = await enforce_user_quota(db, u)
                if quick and not changed:
                    # List sync: skip remote modify (slow) unless status flipped
                    return
                result = await db.execute(
                    select(PanelAccount).where(PanelAccount.user_id == u.id)
                )
                accounts = list(result.scalars().all())
                multi = len(_active_accounts(accounts)) > 1
                if await should_push_remotes(
                    db,
                    u,
                    accounts,
                    status_changed=changed,
                    force=bool(push_remotes) and not quick,
                    shrink_shared_caps=multi,
                ):
                    await sync_modify_user(db, u)
            except Exception as exc:  # noqa: BLE001
                logger.warning("batch usage refresh failed user=%s: %s", u.username, exc)

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
    coeffs = await panel_coefficients_for_user(db, user)

    need_remote = force or _accounts_need_remote_refresh(accounts, interval)
    if need_remote:
        # quick: fail-fast for admin list; full: longer for sub page / open-user
        if quick:
            per_try = 4.5
            tries = 1
        elif force:
            per_try = 18.0
            tries = 2
        else:
            per_try = 8.0
            tries = 2

        async def _remote(account: PanelAccount) -> tuple[PanelAccount, AdapterResult | None]:
            # Pull usage for active (and error — may still exist on remote)
            if not account.panel or account.status not in (
                PanelAccountStatus.active,
                PanelAccountStatus.error,
            ):
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
                    await asyncio.sleep(0.15)
            return account, last

        outcomes = await asyncio.gather(*[_remote(a) for a in accounts])
        now = datetime.now(timezone.utc)
        from app.adapters.http_utils import is_share_link

        ok_n = 0
        for account, res in outcomes:
            if res is None:
                continue
            if res.success:
                ok_n += 1
                # Trust live panel value (source of truth)
                account.used_traffic = int(res.used_traffic or 0)
                # Never replace real share links with raw /sub refs from usage pulls
                new_shares = [
                    str(x) for x in (res.links or []) if x and is_share_link(str(x))
                ]
                if new_shares:
                    account.links = new_shares
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
            "refresh_usage user=%s panels=%s ok=%s force=%s",
            user.username,
            len(accounts),
            ok_n,
            force,
        )

    total = await _weighted_total(accounts, coeffs)
    user.used_traffic = total
    await db.flush()
    return total


async def enforce_user_quota(db: AsyncSession, user: User) -> bool:
    """Apply expire / data-limit status. Returns True if status changed.

    When used_astra (sum of all panels × coeffs) hits data_limit → limited.
    Remotes are disabled via sync_modify even if the upstream panel still
    shows leftover traffic.
    """
    if user.status == UserStatus.disabled:
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


async def resync_users_for_group(db: AsyncSession, group_id: int) -> int:
    """After group coefficient / panel config change: reanchor, enforce, push remotes.

    Ensures remotes get the new (usually lower) data_limit and are disabled when
    Astra quota is already exhausted under the new rate.
    """
    ug = await db.execute(select(UserGroup.user_id).where(UserGroup.group_id == group_id))
    user_ids = [row[0] for row in ug.all()]
    if not user_ids:
        return 0

    result = await db.execute(
        select(User)
        .where(User.id.in_(user_ids))
        .options(selectinload(User.panel_accounts).selectinload(PanelAccount.panel))
    )
    users = list(result.scalars().unique().all())
    # Skip local sample probe users
    from app.services.group_configs import sample_username_for_group

    sample = sample_username_for_group(group_id)
    users = [u for u in users if u.username != sample]
    if not users:
        return 0

    await refresh_users_usage_batch(
        db, users, force=True, concurrency=8, push_remotes=True
    )
    await db.flush()
    logger.info(
        "group %s coeff/config resync: %s users pushed to remotes",
        group_id,
        len(users),
    )
    return len(users)


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
