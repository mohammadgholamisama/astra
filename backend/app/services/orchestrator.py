import asyncio
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

# Per-user locks to avoid stampede on concurrent sub fetches
_user_locks: dict[int, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _lock_for_user(user_id: int) -> asyncio.Lock:
    async with _locks_guard:
        lock = _user_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            _user_locks[user_id] = lock
        return lock


async def resolve_panels_for_groups(db: AsyncSession, group_ids: list[int]) -> list[tuple[Panel, dict]]:
    """Return unique (panel, merged_config) for all panels in given groups."""
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
        if panel.id not in seen:
            seen[panel.id] = (panel, dict(link.config or {}))
        else:
            # merge configs (later groups don't overwrite existing keys)
            merged = {**(link.config or {}), **seen[panel.id][1]}
            seen[panel.id] = (panel, merged)
    return list(seen.values())


def _fail_result(error: str) -> AdapterResult:
    return AdapterResult(
        success=False,
        error=error,
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
    """
    Ensure PanelAccount rows (sequential DB), then create users on remotes in parallel
    (HTTP only), then write results back sequentially.
    """
    if not panels:
        return []

    # 1) Sequential: prepare local account rows + payloads (no concurrent DB use)
    jobs: list[tuple[PanelAccount, Panel, UserPayload]] = []
    for panel, config in panels:
        existing = await db.execute(
            select(PanelAccount).where(
                PanelAccount.user_id == user.id, PanelAccount.panel_id == panel.id
            )
        )
        account = existing.scalar_one_or_none()
        if account is None:
            account = PanelAccount(
                user_id=user.id, panel_id=panel.id, status=PanelAccountStatus.pending
            )
            db.add(account)
            await db.flush()

        cfg = {
            **(config or {}),
            "proxy_settings": user.proxy_settings or {},
            "data_limit_reset_strategy": user.data_limit_reset_strategy,
        }
        # Only pass explicit proxies from user settings — never force vless/vmess.
        # Marzban/Rebecca build proxies from group protocols/inbounds themselves.
        explicit_proxies = (user.proxy_settings or {}).get("proxies")
        if isinstance(explicit_proxies, dict) and explicit_proxies:
            cfg["proxies"] = explicit_proxies

        payload = UserPayload(
            username=user.username,
            data_limit=user.data_limit or 0,
            expire=user.expire or None,
            status=user.status.value,
            note=user.note,
            uuid=account.uuid,
            config=cfg,
        )
        jobs.append((account, panel, payload))

    # 2) Parallel: remote HTTP only
    async def _remote(panel: Panel, payload: UserPayload) -> AdapterResult:
        adapter = get_adapter(panel)
        try:
            return await asyncio.wait_for(adapter.create_user(payload), timeout=45)
        except Exception as e:  # noqa: BLE001
            return _fail_result(str(e))

    results = await asyncio.gather(*[_remote(panel, payload) for _, panel, payload in jobs])

    # 3) Sequential: persist outcomes
    accounts: list[PanelAccount] = []
    now = datetime.now(timezone.utc)
    for (account, panel, payload), result in zip(jobs, results):
        if result.success:
            account.status = PanelAccountStatus.active
            account.remote_username = result.remote_username or user.username
            account.remote_id = result.remote_id
            account.uuid = result.uuid or account.uuid
            account.links = result.links or []
            account.used_traffic = result.used_traffic or 0
            account.error_message = None
            account.last_sync_at = now
            panel.last_ok_at = now
        else:
            # If user already exists on remote, try modify to recover links
            err = (result.error or "").lower()
            if any(x in err for x in ("exist", "already", "duplicate", "تکرار", "موجود")):
                try:
                    adapter = get_adapter(panel)
                    mod = await asyncio.wait_for(adapter.modify_user(payload), timeout=45)
                    if mod.success:
                        account.status = PanelAccountStatus.active
                        account.remote_username = mod.remote_username or user.username
                        account.remote_id = mod.remote_id or account.remote_id
                        account.uuid = mod.uuid or account.uuid
                        account.links = mod.links or account.links or []
                        account.used_traffic = mod.used_traffic or account.used_traffic or 0
                        account.error_message = None
                        account.last_sync_at = now
                        panel.last_ok_at = now
                        accounts.append(account)
                        continue
                    account.status = PanelAccountStatus.error
                    account.error_message = mod.error or result.error
                except Exception as e:  # noqa: BLE001
                    account.status = PanelAccountStatus.error
                    account.error_message = f"{result.error} | modify: {e}"
            else:
                account.status = PanelAccountStatus.error
                account.error_message = result.error
        accounts.append(account)

    await db.flush()
    return accounts


async def sync_modify_user(db: AsyncSession, user: User) -> None:
    result = await db.execute(
        select(PanelAccount)
        .where(PanelAccount.user_id == user.id)
        .options(selectinload(PanelAccount.panel))
    )
    accounts = result.scalars().all()

    # Merge GroupPanel.config (e.g. limit_ip / coefficient) from user's groups
    ug = await db.execute(select(UserGroup.group_id).where(UserGroup.user_id == user.id))
    group_ids = [row[0] for row in ug.all()]
    panel_cfg: dict[int, dict] = {}
    if group_ids:
        for panel, cfg in await resolve_panels_for_groups(db, group_ids):
            panel_cfg[panel.id] = cfg

    jobs: list[tuple[PanelAccount, Panel, UserPayload]] = []
    for account in accounts:
        panel = account.panel
        if not panel or not panel.is_active:
            continue
        merged = dict(panel_cfg.get(panel.id) or {})
        cfg = {
            **merged,
            "proxy_settings": user.proxy_settings or {},
            "data_limit_reset_strategy": user.data_limit_reset_strategy,
        }
        explicit_proxies = (user.proxy_settings or {}).get("proxies")
        if isinstance(explicit_proxies, dict) and explicit_proxies:
            cfg["proxies"] = explicit_proxies
        payload = UserPayload(
            username=user.username,
            data_limit=user.data_limit or 0,
            expire=user.expire or None,
            status=user.status.value,
            note=user.note,
            uuid=account.uuid,
            config=cfg,
        )
        jobs.append((account, panel, payload))

    async def _remote(panel: Panel, payload: UserPayload) -> AdapterResult:
        adapter = get_adapter(panel)
        try:
            return await asyncio.wait_for(adapter.modify_user(payload), timeout=45)
        except Exception as e:  # noqa: BLE001
            return _fail_result(str(e))

    if not jobs:
        return

    results = await asyncio.gather(*[_remote(panel, payload) for _, panel, payload in jobs])
    now = datetime.now(timezone.utc)
    for (account, _panel, _payload), res in zip(jobs, results):
        if res.success:
            account.status = PanelAccountStatus.active
            account.links = res.links or account.links
            account.used_traffic = res.used_traffic or account.used_traffic
            account.error_message = None
            account.last_sync_at = now
        else:
            account.status = PanelAccountStatus.error
            account.error_message = res.error
    await db.flush()


async def sync_delete_user(db: AsyncSession, user: User) -> list[dict]:
    """
    Delete user from every remote panel.
    Returns list of {panel_id, panel_name, ok, error} for API feedback.
    Soft-fails «not found» as success (already gone on remote).
    """
    result = await db.execute(
        select(PanelAccount)
        .where(PanelAccount.user_id == user.id)
        .options(selectinload(PanelAccount.panel))
    )
    accounts = result.scalars().all()
    if not accounts:
        return []

    async def _remote(account: PanelAccount) -> dict:
        panel = account.panel
        name = panel.name if panel else f"#{account.panel_id}"
        if not panel:
            return {
                "panel_id": account.panel_id,
                "panel_name": name,
                "ok": False,
                "error": "پنل پیدا نشد",
            }
        adapter = get_adapter(panel)
        try:
            res = await asyncio.wait_for(
                adapter.delete_user(user.username, account.remote_id or account.remote_username),
                timeout=35,
            )
        except Exception as e:  # noqa: BLE001
            return {
                "panel_id": account.panel_id,
                "panel_name": name,
                "ok": False,
                "error": str(e),
            }

        err = (res.error or "").lower() if not res.success else ""
        already_gone = any(
            x in err for x in ("not found", "404", "does not exist", "وجود ندارد", "پیدا نشد")
        )
        if res.success or already_gone:
            account.status = PanelAccountStatus.deleted
            account.error_message = None
            return {
                "panel_id": account.panel_id,
                "panel_name": name,
                "ok": True,
                "error": None,
            }

        account.error_message = res.error
        return {
            "panel_id": account.panel_id,
            "panel_name": name,
            "ok": False,
            "error": res.error or "حذف از پنل ناموفق بود",
        }

    outcomes = list(await asyncio.gather(*[_remote(a) for a in accounts]))
    await db.flush()
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
    panels = [(a.panel, {}) for a in failed if a.panel]
    return await provision_user_on_panels(db, user, panels)


async def collect_links(user: User) -> list[str]:
    return [row["link"] for row in collect_links_detailed(user)]


def collect_links_detailed(user: User) -> list[dict]:
    """All unique share links across panels.

    Group notices are prepended first.
    Real config remarks: «اسم 🟢 حجم‌باقی #آیدی» (no panel name).
    """
    from app.services.link_remark import apply_display_name, notice_rows_from_groups

    notices = notice_rows_from_groups(list(user.__dict__.get("groups") or []))
    rows: list[dict] = []
    seen: set[str] = set()
    for account in user.panel_accounts or []:
        if account.status != PanelAccountStatus.active:
            continue
        panel = account.__dict__.get("panel")
        panel_name = getattr(panel, "name", None) if panel is not None else None
        label = panel_name or f"پنل #{account.panel_id}"
        for link in account.links or []:
            if link and link not in seen:
                seen.add(link)
                rows.append({"link": link, "panel": label, "panel_id": account.panel_id})
    renamed = apply_display_name(
        rows,
        getattr(user, "display_name", None),
        used=int(user.used_traffic or 0),
        limit=int(user.data_limit or 0),
        username=getattr(user, "username", None),
    )
    return notices + renamed


async def usage_breakdown_for_user(db: AsyncSession, user: User) -> list[dict]:
    """Per-panel raw usage × coefficient → weighted (for sub page chart)."""
    coeffs = await panel_coefficients_for_user(db, user)
    rows: list[dict] = []
    for account in user.panel_accounts or []:
        if account.status != PanelAccountStatus.active:
            continue
        panel = account.__dict__.get("panel")
        name = getattr(panel, "name", None) if panel is not None else None
        label = name or f"پنل #{account.panel_id}"
        raw = int(account.used_traffic or 0)
        coeff = float(coeffs.get(account.panel_id, 1.0) or 1.0)
        if coeff < 0.01:
            coeff = 0.01
        weighted = int(round(raw * coeff))
        n_links = len([x for x in (account.links or []) if x])
        rows.append(
            {
                "panel": label,
                "panel_id": account.panel_id,
                "raw": raw,
                "coefficient": coeff,
                "weighted": weighted,
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


async def _weighted_total(
    accounts: list[PanelAccount], coeffs: dict[int, float]
) -> int:
    total = 0
    for account in accounts:
        if account.status != PanelAccountStatus.active:
            continue
        raw = int(account.used_traffic or 0)
        coeff = float(coeffs.get(account.panel_id, 1.0) or 1.0)
        total += int(round(raw * coeff))
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


async def refresh_usage(
    db: AsyncSession,
    user: User,
    *,
    force: bool = False,
    min_interval: int | None = None,
) -> int:
    """Refresh remote usage and apply per-panel Astra coefficients.

    PanelAccount.used_traffic stays raw (from remote).
    User.used_traffic = sum(raw_i * coefficient_i) across active panels.

    When not force and last sync is fresh (< min_interval), only recompute
    weighted total from cached raw values (no remote calls).
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

        async def _remote(account: PanelAccount) -> tuple[PanelAccount, AdapterResult | None]:
            if not account.panel or account.status != PanelAccountStatus.active:
                return account, None
            adapter = get_adapter(account.panel)
            try:
                res = await asyncio.wait_for(
                    adapter.get_usage(user.username, account.remote_id),
                    timeout=20,
                )
                return account, res
            except Exception:  # noqa: BLE001
                return account, None

        outcomes = await asyncio.gather(*[_remote(a) for a in accounts])
        now = datetime.now(timezone.utc)
        for account, res in outcomes:
            if res and res.success:
                account.used_traffic = int(res.used_traffic or 0)
                if res.links:
                    account.links = res.links
                account.last_sync_at = now

    total = await _weighted_total(accounts, coeffs)
    user.used_traffic = total
    await db.flush()
    return total


async def enforce_user_quota(db: AsyncSession, user: User) -> bool:
    """Apply expire / data-limit status. Returns True if remotes must be synced.

    - expired / limited → disable on upstream panels
    - recovers to active when under limit again (not for admin-disabled)
    """
    if user.status == UserStatus.disabled:
        return False

    now = int(time.time())
    old = user.status
    new = old

    expired = bool(user.expire and int(user.expire) > 0 and now >= int(user.expire))
    limited = bool(
        user.data_limit
        and int(user.data_limit) > 0
        and int(user.used_traffic or 0) >= int(user.data_limit)
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
    return True


async def sync_subscription_state(
    db: AsyncSession,
    user: User,
    *,
    force: bool = False,
) -> User:
    """Throttled usage refresh + quota enforce + remote disable/enable.

    Safe under concurrent sub clients via per-user lock.
    """
    lock = await _lock_for_user(user.id)
    async with lock:
        await refresh_usage(db, user, force=force)
        changed = await enforce_user_quota(db, user)
        # Retry remote push if previous disable/enable left accounts in error
        result = await db.execute(
            select(PanelAccount).where(PanelAccount.user_id == user.id)
        )
        accounts = list(result.scalars().all())
        has_err = any(a.status == PanelAccountStatus.error for a in accounts)
        blocked = user.status in (
            UserStatus.limited,
            UserStatus.expired,
            UserStatus.disabled,
        )
        if changed or (blocked and has_err):
            await sync_modify_user(db, user)
        await db.flush()
        return user
