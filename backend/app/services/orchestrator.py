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
from app.services import quota as quota_math

logger = logging.getLogger("astra.usage")

# Per-user locks to avoid stampede on concurrent sub fetches
_user_locks: dict[int, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


def coefficient_from_config(config: dict | None) -> float:
    return quota_math.norm_coeff((config or {}).get("coefficient", 1))


def _norm_coeff(raw: float | None, default: float = 1.0) -> float:
    return quota_math.norm_coeff(raw, default)


def account_weighted_usage(account: PanelAccount, coeff: float) -> int:
    """Astra-facing usage for one panel: (anchor + (raw-anchor)×coeff)."""
    return quota_math.weighted_bytes(
        int(account.used_traffic or 0),
        coeff,
        anchor_raw=int(getattr(account, "coeff_anchor_raw", 0) or 0),
        anchor_weighted=int(getattr(account, "coeff_anchor_weighted", 0) or 0),
    )


def apply_coefficient_period(account: PanelAccount, new_coeff: float) -> bool:
    """Lock current weighted usage when the panel coefficient changes."""
    new_c = _norm_coeff(new_coeff)
    raw = max(0, int(account.used_traffic or 0))
    anchor_raw = max(0, int(getattr(account, "coeff_anchor_raw", 0) or 0))
    if raw < anchor_raw:
        account.coeff_anchor_raw = 0
        account.coeff_anchor_weighted = 0
        account.applied_coefficient = new_c
        return True

    applied = getattr(account, "applied_coefficient", None)
    if applied is None:
        account.applied_coefficient = new_c
        return True

    old_c = _norm_coeff(applied)
    if abs(old_c - new_c) < 1e-9:
        return False

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
    """Absolute traffic ceiling to push to one remote panel (shared Astra remaining)."""
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
    status = _remote_status_for_user(user)
    data_limit = remote_data_limit_bytes(
        user.data_limit or 0,
        cfg,
        used_weighted_total=used_w,
        used_raw_on_panel=used_raw_on_panel,
    )
    # When Astra sale is finished, freeze remote at current raw regardless of math edge cases
    if status in ("limited", "expired", "disabled"):
        used_r = max(0, int(used_raw_on_panel or 0))
        if int(user.data_limit or 0) > 0:
            data_limit = max(1, used_r) if used_r > 0 else 1
    return UserPayload(
        username=user.username,
        data_limit=data_limit,
        expire=user.expire or None,
        status=status,
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
            # Adapter already retries; keep outer budget high so we don't cancel
            # AFTER Marzban accepted the user but before the response returns.
            return await asyncio.wait_for(adapter.create_user(payload), timeout=120)
        except Exception as e:  # noqa: BLE001
            # Outer timeout / cancel — still try to confirm remote exists
            err = _exc_msg(e)
            try:
                for _ in range(5):
                    try:
                        usage = await asyncio.wait_for(
                            adapter.get_usage(payload.username), timeout=15
                        )
                        if usage.success:
                            return usage
                    except Exception:
                        pass
                    await asyncio.sleep(0.4)
            except Exception:
                pass
            return _fail_result(err)
        finally:
            try:
                await adapter.close()  # type: ignore[attr-defined]
            except Exception:
                pass

    results = await asyncio.gather(*[_remote(panel, payload) for _, panel, payload in jobs])

    # 3) Sequential: persist outcomes (+ recover timeout/exists without needing UI retry)
    accounts: list[PanelAccount] = []
    now = datetime.now(timezone.utc)
    for (account, panel, payload), result in zip(jobs, results):
        cfg = dict(payload.config or {})
        coeff = coefficient_from_config(cfg)

        async def _mark_ok(res: AdapterResult) -> None:
            account.status = PanelAccountStatus.active
            account.remote_username = res.remote_username or user.username
            account.remote_id = res.remote_id or account.remote_id
            account.uuid = res.uuid or account.uuid
            account.links = res.links or account.links or []
            account.used_traffic = int(res.used_traffic or account.used_traffic or 0)
            account.coeff_anchor_raw = 0
            account.coeff_anchor_weighted = 0
            account.applied_coefficient = coeff
            account.error_message = None
            account.last_sync_at = now
            panel.last_ok_at = now

        if result.success:
            await _mark_ok(result)
        else:
            err = (result.error or "").lower()
            exists_hint = any(
                x in err
                for x in (
                    "exist",
                    "already",
                    "duplicate",
                    "تکرار",
                    "موجود",
                    "timeout",
                    "connect",
                    "network",
                    "قطع",
                )
            )
            recovered = False
            # Always try recover for timeout/network — user often exists on Marzban
            if exists_hint or not err:
                adapter = get_adapter(panel)
                try:
                    usage = None
                    for _ in range(5):
                        try:
                            usage = await asyncio.wait_for(
                                adapter.get_usage(payload.username), timeout=18
                            )
                            if usage.success:
                                break
                        except Exception:
                            usage = None
                        await asyncio.sleep(0.4)
                    if usage and usage.success:
                        # Prefer modify to sync limits; usage alone is enough for OK
                        try:
                            mod = await asyncio.wait_for(
                                adapter.modify_user(payload), timeout=40
                            )
                            await _mark_ok(mod if mod.success else usage)
                        except Exception:
                            await _mark_ok(usage)
                        recovered = True
                    else:
                        retry = await asyncio.wait_for(
                            adapter.create_user(payload), timeout=90
                        )
                        if retry.success:
                            await _mark_ok(retry)
                            recovered = True
                        else:
                            err2 = (retry.error or "").lower()
                            if any(
                                x in err2
                                for x in ("exist", "already", "duplicate", "تکرار", "موجود")
                            ):
                                mod = await asyncio.wait_for(
                                    adapter.modify_user(payload), timeout=40
                                )
                                if mod.success:
                                    await _mark_ok(mod)
                                    recovered = True
                                else:
                                    account.status = PanelAccountStatus.error
                                    account.error_message = (
                                        mod.error or retry.error or result.error
                                    )
                            else:
                                account.status = PanelAccountStatus.error
                                account.error_message = (
                                    retry.error or result.error or "ایجاد در پنل ناموفق"
                                )
                except Exception as e:  # noqa: BLE001
                    account.status = PanelAccountStatus.error
                    account.error_message = (
                        f"{result.error or 'create failed'} | recover: {_exc_msg(e)}"
                    )
                finally:
                    try:
                        await adapter.close()  # type: ignore[attr-defined]
                    except Exception:
                        pass
            if not recovered and account.status != PanelAccountStatus.active:
                account.status = PanelAccountStatus.error
                account.error_message = result.error or "ایجاد در پنل ناموفق"
        accounts.append(account)

    await db.flush()
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
        # Still push disable/limits to error accounts — they often exist on remote
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
        attempts = 3 if blocked else 1
        last = _fail_result("modify failed")
        for i in range(attempts):
            adapter = get_adapter(panel)
            try:
                last = await asyncio.wait_for(adapter.modify_user(payload), timeout=55)
                if last.success:
                    return last
            except Exception as e:  # noqa: BLE001
                last = _fail_result(_exc_msg(e))
            finally:
                try:
                    await adapter.close()  # type: ignore[attr-defined]
                except Exception:
                    pass
            if i + 1 < attempts:
                await asyncio.sleep(0.35 + i * 0.25)
        return last

    if not jobs:
        await db.flush()
        return

    results = await asyncio.gather(*[_remote(panel, payload) for _, panel, payload in jobs])
    now = datetime.now(timezone.utc)
    for (account, _panel, _payload), res in zip(jobs, results):
        if res.success:
            # Account row stays "active" (= provisioned); traffic cut is user.status
            if account.status != PanelAccountStatus.error:
                account.status = PanelAccountStatus.active
            elif blocked:
                # Disable push succeeded — clear stale create error
                account.status = PanelAccountStatus.active
            account.links = res.links or account.links
            if res.used_traffic is not None:
                account.used_traffic = int(res.used_traffic or account.used_traffic or 0)
            account.error_message = None
            account.last_sync_at = now
        else:
            account.status = PanelAccountStatus.error
            account.error_message = res.error or "به‌روزرسانی پنل ناموفق"
            if blocked:
                logger.warning(
                    "failed to freeze remote user=%s panel=%s: %s",
                    user.username,
                    account.panel_id,
                    account.error_message,
                )
    await db.flush()


async def sync_delete_user(db: AsyncSession, user: User) -> list[dict]:
    """
    Delete user on every remote panel (docs DELETE). Strict result:

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
    accounts = result.scalars().all()
    if not accounts:
        return []

    async def _remote(account: PanelAccount) -> dict:
        panel = account.panel
        name = panel.name if panel else f"#{account.panel_id}"
        uname = (account.remote_username or user.username or "").strip()

        if not panel:
            return {
                "panel_id": account.panel_id,
                "panel_name": name,
                "ok": False,
                "error": "پنل پیدا نشد — حذف لغو شد",
            }

        if not uname:
            return {
                "panel_id": account.panel_id,
                "panel_name": name,
                "ok": False,
                "error": "یوزرنیم ریموت خالی است",
            }

        adapter = get_adapter(panel)
        try:
            res = await asyncio.wait_for(
                adapter.delete_user(uname, account.remote_id or account.remote_username),
                timeout=45,
            )
        except Exception as e:  # noqa: BLE001
            try:
                await adapter.close()  # type: ignore[attr-defined]
            except Exception:
                pass
            account.error_message = _exc_msg(e)
            return {
                "panel_id": account.panel_id,
                "panel_name": name,
                "ok": False,
                "error": account.error_message,
            }

        try:
            await adapter.close()  # type: ignore[attr-defined]
        except Exception:
            pass

        if res.success:
            account.status = PanelAccountStatus.deleted
            account.error_message = None
            return {
                "panel_id": account.panel_id,
                "panel_name": name,
                "ok": True,
                "error": None,
            }

        account.error_message = res.error or "حذف از پنل ناموفق بود"
        return {
            "panel_id": account.panel_id,
            "panel_name": name,
            "ok": False,
            "error": account.error_message,
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
    """Fetch+expand share links for every panel account that still lacks them.

    Runs panels in parallel so multi-group subs get Marzban+PasarGuard+Sanaei+Rebecca
    configs together — not only the first panel that already had vless:// links.
    """
    from app.adapters.http_utils import is_share_link, is_subscription_ref

    targets: list[PanelAccount] = []
    for account in user.panel_accounts or []:
        if account.status not in (
            PanelAccountStatus.active,
            PanelAccountStatus.error,
            PanelAccountStatus.pending,
        ):
            continue
        if not account.panel:
            continue
        links = [str(x).strip() for x in (account.links or []) if x and str(x).strip()]
        if any(is_share_link(x) for x in links):
            if account.status == PanelAccountStatus.error:
                account.status = PanelAccountStatus.active
            continue
        # Empty, or only /sub/... / https subscription refs
        if links and not any(is_subscription_ref(x) for x in links):
            continue
        targets.append(account)

    if not targets:
        await db.flush()
        return 0

    async def _one(account: PanelAccount) -> bool:
        adapter = get_adapter(account.panel)
        uname = (account.remote_username or user.username or "").strip()
        rid = account.remote_id or account.remote_username or uname or None
        res = None
        try:
            try:
                coro = adapter.get_usage(uname, rid, expand_links=True)  # type: ignore[call-arg]
            except TypeError:
                coro = adapter.get_usage(uname, rid)
            res = await asyncio.wait_for(coro, timeout=25)
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
        if not res or not res.success:
            return False
        new_links = [x for x in (res.links or []) if is_share_link(str(x))]
        if not new_links:
            # Keep subscription refs for a later pass; mark active if remote exists
            if res.links:
                account.links = list(res.links)
            account.status = PanelAccountStatus.active
            account.error_message = None
            account.remote_username = res.remote_username or account.remote_username or uname
            account.used_traffic = int(res.used_traffic or account.used_traffic or 0)
            account.last_sync_at = datetime.now(timezone.utc)
            return True
        account.links = new_links
        account.status = PanelAccountStatus.active
        account.error_message = None
        account.remote_username = res.remote_username or account.remote_username or uname
        account.remote_id = res.remote_id or account.remote_id
        account.used_traffic = int(res.used_traffic or account.used_traffic or 0)
        account.last_sync_at = datetime.now(timezone.utc)
        return True

    outcomes = await asyncio.gather(*[_one(a) for a in targets])
    updated = sum(1 for ok in outcomes if ok)
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
) -> None:
    """Live-pull usage for many users. Push remotes when shared caps / disable needed."""
    if not users:
        return
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(u: User) -> None:
        async with sem:
            try:
                await refresh_usage(db, u, force=force)
                changed = await enforce_user_quota(db, u)
                result = await db.execute(
                    select(PanelAccount).where(PanelAccount.user_id == u.id)
                )
                accounts = list(result.scalars().all())
                multi = len(_active_accounts(accounts)) > 1
                # Multi-panel sales MUST shrink shared remaining (Σ raw×coeff),
                # otherwise each remote keeps the full Astra ceiling.
                if await should_push_remotes(
                    db,
                    u,
                    accounts,
                    status_changed=changed,
                    force=bool(push_remotes),
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
