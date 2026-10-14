import asyncio
import logging
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

logger = logging.getLogger("astra.usage")

# Per-user locks to avoid stampede on concurrent sub fetches
_user_locks: dict[int, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


def coefficient_from_config(config: dict | None) -> float:
    raw = (config or {}).get("coefficient", 1)
    try:
        c = float(raw if raw is not None else 1)
    except (TypeError, ValueError):
        c = 1.0
    if c < 0.01:
        c = 0.01
    return c


def _norm_coeff(raw: float | None, default: float = 1.0) -> float:
    try:
        c = float(raw if raw is not None else default)
    except (TypeError, ValueError):
        c = default
    if c < 0.01:
        c = 0.01
    return c


def account_weighted_usage(account: PanelAccount, coeff: float) -> int:
    """Astra-facing usage for one panel account under the current coefficient.

    Past traffic locked in coeff_anchor_* keeps the old rate; only (raw - anchor)
    is multiplied by the live coefficient — so changing 1→2 speeds up usage
    from that moment onward without rewriting history.
    """
    coeff = _norm_coeff(coeff)
    raw = max(0, int(account.used_traffic or 0))
    anchor_raw = max(0, int(getattr(account, "coeff_anchor_raw", 0) or 0))
    anchor_w = max(0, int(getattr(account, "coeff_anchor_weighted", 0) or 0))
    if raw < anchor_raw:
        # Traffic reset on remote — drop the period lock
        return int(round(raw * coeff))
    return anchor_w + int(round((raw - anchor_raw) * coeff))


def apply_coefficient_period(
    account: PanelAccount, new_coeff: float
) -> bool:
    """Lock current weighted usage when the panel coefficient changes.

    Returns True if anchors / applied_coefficient were updated.
    """
    new_c = _norm_coeff(new_coeff)
    raw = max(0, int(account.used_traffic or 0))
    anchor_raw = max(0, int(getattr(account, "coeff_anchor_raw", 0) or 0))
    # Remote traffic reset → clear period lock
    if raw < anchor_raw:
        account.coeff_anchor_raw = 0
        account.coeff_anchor_weighted = 0
        account.applied_coefficient = new_c
        return True

    applied = getattr(account, "applied_coefficient", None)
    if applied is None:
        # First touch after upgrade: keep simple raw×coeff (anchors at 0)
        account.applied_coefficient = new_c
        return True

    old_c = _norm_coeff(applied)
    if abs(old_c - new_c) < 1e-9:
        return False

    # Finalize weighted under the OLD coefficient, then start a new period
    locked = account_weighted_usage(account, old_c)
    account.coeff_anchor_raw = raw
    account.coeff_anchor_weighted = max(0, locked)
    account.applied_coefficient = new_c
    return True


def remote_data_limit_bytes(
    user_limit: int,
    config: dict | None,
    *,
    used_weighted_total: int = 0,
    used_raw_on_panel: int = 0,
) -> int:
    """Absolute traffic ceiling to push to one remote panel.

    Astra quota is SHARED across all panels:
      user.used_traffic = Σ weighted_i  (anchor + delta×coeff)
      when that sum hits data_limit → subscription ends everywhere
      (remotes get status limited/disabled + data_limit frozen).

    Each remote only gets: already_used_on_panel + remaining_astra/coeff
    so one panel cannot silently consume the whole quota on every panel.
    """
    limit = int(user_limit or 0)
    if limit <= 0:
        return 0  # unlimited on Astra → unlimited on remote
    coeff = coefficient_from_config(config)
    used_w = max(0, int(used_weighted_total or 0))
    used_r = max(0, int(used_raw_on_panel or 0))
    remaining_w = max(0, limit - used_w)
    if remaining_w <= 0:
        # Quota exhausted — freeze remote at current usage (no more traffic)
        return max(1, used_r) if used_r > 0 else 1
    more_raw = int(remaining_w / coeff)
    return max(1, used_r + more_raw)


def build_user_payload(
    user: User,
    *,
    uuid: str | None,
    panel_config: dict | None,
    used_weighted_total: int | None = None,
    used_raw_on_panel: int = 0,
) -> UserPayload:
    """Build remote payload; data_limit uses shared remaining Astra quota."""
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
    return UserPayload(
        username=user.username,
        data_limit=remote_data_limit_bytes(
            user.data_limit or 0,
            cfg,
            used_weighted_total=used_w,
            used_raw_on_panel=used_raw_on_panel,
        ),
        expire=user.expire or None,
        status=user.status.value,
        note=user.note,
        uuid=uuid,
        config=cfg,
    )


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

        # Only pass explicit proxies from user settings — never force vless/vmess.
        # data_limit on remote = Astra limit ÷ panel coefficient
        payload = build_user_payload(user, uuid=account.uuid, panel_config=config)
        jobs.append((account, panel, payload))

    # 2) Parallel: remote HTTP only
    async def _remote(panel: Panel, payload: UserPayload) -> AdapterResult:
        adapter = get_adapter(panel)
        try:
            return await asyncio.wait_for(adapter.create_user(payload), timeout=28)
        except Exception as e:  # noqa: BLE001
            return _fail_result(_exc_msg(e))

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
            # Recover: already-exists / create timed out after remote accepted
            err = (result.error or "").lower()
            exists_hint = any(
                x in err for x in ("exist", "already", "duplicate", "تکرار", "موجود", "timeout")
            )
            recovered = False
            if exists_hint or not err:
                try:
                    adapter = get_adapter(panel)
                    mod = await asyncio.wait_for(adapter.modify_user(payload), timeout=22)
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
                        recovered = True
                    else:
                        usage = await asyncio.wait_for(
                            adapter.get_usage(payload.username), timeout=12
                        )
                        if usage.success:
                            account.status = PanelAccountStatus.active
                            account.remote_username = usage.remote_username or user.username
                            account.remote_id = usage.remote_id or account.remote_id
                            account.uuid = usage.uuid or account.uuid
                            account.links = usage.links or account.links or []
                            account.used_traffic = usage.used_traffic or 0
                            account.error_message = None
                            account.last_sync_at = now
                            panel.last_ok_at = now
                            recovered = True
                        else:
                            account.status = PanelAccountStatus.error
                            account.error_message = (
                                mod.error or usage.error or result.error or "ایجاد در پنل ناموفق"
                            )
                except Exception as e:  # noqa: BLE001
                    account.status = PanelAccountStatus.error
                    account.error_message = f"{result.error or 'create failed'} | recover: {_exc_msg(e)}"
            if not recovered and account.status != PanelAccountStatus.active:
                account.status = PanelAccountStatus.error
                account.error_message = result.error or "ایجاد در پنل ناموفق"
        accounts.append(account)

    await db.flush()
    return accounts


async def sync_modify_user(db: AsyncSession, user: User) -> None:
    """Push status + shared remaining quota to every remote panel account."""
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

    jobs: list[tuple[PanelAccount, Panel, UserPayload]] = []
    for account in accounts:
        panel = account.panel
        if not panel or not panel.is_active:
            continue
        if account.status == PanelAccountStatus.deleted:
            continue
        merged = dict(panel_cfg.get(panel.id) or {})
        payload = build_user_payload(
            user,
            uuid=account.uuid,
            panel_config=merged,
            used_weighted_total=weighted,
            used_raw_on_panel=int(account.used_traffic or 0),
        )
        jobs.append((account, panel, payload))

    async def _remote(panel: Panel, payload: UserPayload) -> AdapterResult:
        adapter = get_adapter(panel)
        try:
            return await asyncio.wait_for(adapter.modify_user(payload), timeout=22)
        except Exception as e:  # noqa: BLE001
            return _fail_result(_exc_msg(e))

    if not jobs:
        await db.flush()
        return

    results = await asyncio.gather(*[_remote(panel, payload) for _, panel, payload in jobs])
    now = datetime.now(timezone.utc)
    for (account, _panel, _payload), res in zip(jobs, results):
        if res.success:
            # Successful push clears prior error — remotes get disabled via status field
            account.status = PanelAccountStatus.active
            account.links = res.links or account.links
            if res.used_traffic is not None:
                account.used_traffic = int(res.used_traffic or account.used_traffic or 0)
            account.error_message = None
            account.last_sync_at = now
        else:
            # Keep links if we already have them; still mark error for UI
            account.status = PanelAccountStatus.error
            account.error_message = res.error or "به‌روزرسانی پنل ناموفق"
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
                timeout=15,
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


async def collect_links(user: User, *, include_notices: bool = False) -> list[str]:
    return [
        row["link"]
        for row in collect_links_detailed(user, include_notices=include_notices)
        if row.get("link")
    ]


def collect_links_detailed(user: User, *, include_notices: bool = True) -> list[dict]:
    """All unique share links across panels.

    Notices (fake configs) are for the HTML sub page only — omit them from
    client subscription feeds so V2Ray apps import real configs cleanly.
    Real config remarks: «اسم 🟢 حجم‌باقی #آیدی» (no panel name).

    Only protocol share links (vless:// …) are returned — never raw /sub/ paths.
    """
    from app.adapters.http_utils import is_share_link
    from app.services.link_remark import apply_display_name, notice_rows_from_groups

    notices = (
        notice_rows_from_groups(list(user.__dict__.get("groups") or []))
        if include_notices
        else []
    )
    rows: list[dict] = []
    seen: set[str] = set()
    for account in user.panel_accounts or []:
        # error accounts often still have working share links (sync flag only) —
        # skipping them made client feeds return Content-Length: 0 while HTML looked fine after refresh.
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
        used=int(user.used_traffic or 0),
        limit=int(user.data_limit or 0),
        username=getattr(user, "username", None),
    )
    return notices + renamed


async def hydrate_share_links(db: AsyncSession, user: User) -> int:
    """
    If panel accounts only stored /sub/... or https subscription URLs,
    fetch remote user and expand into real share links. Returns updated account count.
    """
    from app.adapters.http_utils import is_share_link, is_subscription_ref

    updated = 0
    for account in user.panel_accounts or []:
        if account.status not in (PanelAccountStatus.active, PanelAccountStatus.error, PanelAccountStatus.pending):
            continue
        if not account.panel:
            continue
        links = [str(x).strip() for x in (account.links or []) if x and str(x).strip()]
        # Already have real share links — keep them (even if account flagged error)
        if any(is_share_link(x) for x in links):
            if account.status == PanelAccountStatus.error:
                account.status = PanelAccountStatus.active
                updated += 1
            continue
        # Empty or only /sub/... refs → fetch & expand from remote panel
        if links and not any(is_subscription_ref(x) for x in links):
            continue
        adapter = get_adapter(account.panel)
        uname = (account.remote_username or user.username or "").strip()
        rid = account.remote_id or account.remote_username or uname or None
        res = None
        try:
            res = await asyncio.wait_for(adapter.get_usage(uname, rid), timeout=12)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hydrate links failed user=%s panel=%s: %s",
                user.username,
                account.panel_id,
                exc,
            )
            continue
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    pass
        if not res or not res.success:
            continue
        new_links = [x for x in (res.links or []) if is_share_link(str(x))]
        if not new_links:
            continue
        account.links = new_links
        account.status = PanelAccountStatus.active
        account.used_traffic = int(res.used_traffic or account.used_traffic or 0)
        account.last_sync_at = datetime.now(timezone.utc)
        updated += 1
    if updated:
        await db.flush()
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


async def refresh_users_usage_batch(
    db: AsyncSession,
    users: list[User],
    *,
    force: bool = False,
    concurrency: int = 10,
    push_remotes: bool = False,
) -> None:
    """Live-pull usage for many users (admin users page). Bounded parallelism.

    By default only reads usage (fast). Push to remotes only when quota status
    changes, or when push_remotes=True (manual full sync).
    """
    if not users:
        return
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(u: User) -> None:
        async with sem:
            try:
                await refresh_usage(db, u, force=force)
                changed = await enforce_user_quota(db, u)
                blocked = u.status in (
                    UserStatus.limited,
                    UserStatus.expired,
                    UserStatus.disabled,
                )
                # Push when quota flipped, already blocked (retry disable), or forced
                if changed or blocked or push_remotes:
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
) -> int:
    """Pull live usage from remote panels and apply Astra coefficients.

    PanelAccount.used_traffic = raw bytes from panel.
    User.used_traffic = sum(raw_i * coefficient_i)  (what the end-user sees).

    Remotes already received (quota ÷ coeff) as their data_limit, so when a
    coeff-2 panel reports 500MB used, Astra shows 1GB — matching the user’s quota.
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
            # Pull usage for active (and error — may still exist on remote)
            if not account.panel or account.status not in (
                PanelAccountStatus.active,
                PanelAccountStatus.error,
            ):
                return account, None
            if not (account.remote_username or account.remote_id or user.username):
                return account, None
            adapter = get_adapter(account.panel)
            uname = (account.remote_username or user.username or "").strip()
            rid = (account.remote_id or account.remote_username or uname or None)
            try:
                # Usage-only: skip subscription expand (was the main sync bottleneck)
                try:
                    coro = adapter.get_usage(uname, rid, expand_links=False)  # type: ignore[call-arg]
                except TypeError:
                    coro = adapter.get_usage(uname, rid)
                res = await asyncio.wait_for(coro, timeout=12 if force else 6)
                return account, res
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "get_usage exception user=%s panel=%s: %s",
                    user.username,
                    account.panel_id,
                    _exc_msg(exc),
                )
                return account, None

        outcomes = await asyncio.gather(*[_remote(a) for a in accounts])
        now = datetime.now(timezone.utc)
        from app.adapters.http_utils import is_share_link

        for account, res in outcomes:
            if res is None:
                continue
            if res.success:
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
            else:
                logger.warning(
                    "get_usage failed user=%s panel=%s: %s",
                    user.username,
                    account.panel_id,
                    res.error,
                )

    total = await _weighted_total(accounts, coeffs)
    user.used_traffic = total
    await db.flush()
    return total


async def enforce_user_quota(db: AsyncSession, user: User) -> bool:
    """Apply expire / data-limit status. Returns True if remotes must be synced.

    - expired / limited → disable on upstream panels (even if remote still has quota)
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
      None  → push only when quota/status changed or account errors (fast path for sub)
      True  → always push remaining caps
      False → never push (usage read only)
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
        blocked = user.status in (
            UserStatus.limited,
            UserStatus.expired,
            UserStatus.disabled,
        )
        do_push = push_remotes
        if do_push is None:
            # Always push when blocked so remotes stay disabled even if Astra
            # finished earlier than the upstream panel's own remaining quota.
            do_push = bool(changed or blocked or has_err)
        if do_push:
            await sync_modify_user(db, user)
        await db.flush()
        return user
