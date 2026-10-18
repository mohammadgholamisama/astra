"""Global subscription link order keyed by host:port[+match_host]."""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.adapters.factory import get_adapter
from app.adapters.http_utils import is_share_link, is_subscription_ref
from app.models.config_link_order import ConfigLinkOrder
from app.models.group import Group, GroupPanel
from app.models.panel import Panel, PanelType
from app.models.user import PanelAccount, PanelAccountStatus, User, UserStatus
from app.schemas.config_order import (
    ConfigOrderItemIn,
    ConfigOrderItemOut,
    ConfigOrderWorkspaceOut,
)
from app.services import orchestrator
from app.services.config_locations import (
    extract_link_endpoint,
    extract_link_match_host,
    normalize_host,
    normalize_match_host,
    try_normalize_endpoint,
)
from app.services.group_configs import _parse_link_meta

logger = logging.getLogger(__name__)

# Dedicated probe on every active panel — only for Config Order page.
ORDER_SAMPLE_USERNAME = "astra_order_sample"
ORDER_SAMPLE_NOTE = "نمونه ترتیب کانفیگ — حذف دستی نکنید"

_lock = threading.RLock()
# (host, port, match_host) → sort index (0-based)
_order_idx: dict[tuple[str, int, str], int] = {}
_loaded = False


def _fp_key(host: str, port: int, match_host: str = "") -> tuple[str, int, str]:
    return (host, int(port), match_host or "")


def apply_cache(rows: list[ConfigLinkOrder]) -> None:
    next_map: dict[tuple[str, int, str], int] = {}
    ordered = sorted(rows, key=lambda r: (int(r.sort_order or 0), int(r.id or 0)))
    for i, row in enumerate(ordered):
        try:
            host = normalize_host(row.host)
            port = int(row.port)
            match = normalize_match_host(getattr(row, "match_host", None) or "")
        except (ValueError, TypeError):
            continue
        if port < 1 or port > 65535:
            continue
        next_map[_fp_key(host, port, match)] = i
    with _lock:
        global _order_idx, _loaded
        _order_idx = next_map
        _loaded = True


async def refresh_cache(db: AsyncSession) -> int:
    result = await db.execute(select(ConfigLinkOrder))
    rows = list(result.scalars().all())
    apply_cache(rows)
    return len(rows)


def order_index_map() -> dict[tuple[str, int, str], int]:
    with _lock:
        return dict(_order_idx)


def link_order_index(link: str) -> int:
    """Sort rank for a share link; large number if not in the global list."""
    ep = extract_link_endpoint(link)
    if not ep:
        return 10**6
    host, port = ep
    match = extract_link_match_host(link) or ""
    norm = try_normalize_endpoint(host, port, match)
    if not norm:
        return 10**6
    host, port, match = norm
    with _lock:
        return _order_idx.get(_fp_key(host, port, match), 10**6)


def _normalize_label(raw: str | None) -> str | None:
    text = " ".join(str(raw or "").split()).strip()
    if not text:
        return None
    return text[:128]


async def _get_order_sample_user(db: AsyncSession) -> User | None:
    result = await db.execute(
        select(User)
        .where(User.username == ORDER_SAMPLE_USERNAME)
        .options(selectinload(User.panel_accounts).selectinload(PanelAccount.panel))
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _ensure_order_sample_user(db: AsyncSession) -> User:
    user = await _get_order_sample_user(db)
    if user is not None:
        if user.note != ORDER_SAMPLE_NOTE:
            user.note = ORDER_SAMPLE_NOTE
        if user.status != UserStatus.active:
            user.status = UserStatus.active
        await db.flush()
        return user
    user = User(
        username=ORDER_SAMPLE_USERNAME,
        status=UserStatus.active,
        data_limit=0,
        expire=0,
        note=ORDER_SAMPLE_NOTE,
        proxy_settings={},
    )
    db.add(user)
    await db.flush()
    reloaded = await _get_order_sample_user(db)
    return reloaded or user


async def _resolve_all_active_panels(
    db: AsyncSession,
) -> list[tuple[Panel, dict, int]]:
    """One target per active panel; merge GroupPanel configs so all inbounds appear."""
    panels = list(
        (
            await db.execute(
                select(Panel).where(Panel.is_active.is_(True)).order_by(Panel.id)
            )
        )
        .scalars()
        .all()
    )
    if not panels:
        return []

    links = list(
        (
            await db.execute(
                select(GroupPanel)
                .join(Group, Group.id == GroupPanel.group_id)
                .where(
                    GroupPanel.panel_id.in_([p.id for p in panels]),
                    Group.is_disabled.is_(False),
                )
                .order_by(GroupPanel.priority.desc(), GroupPanel.id.asc())
            )
        )
        .scalars()
        .all()
    )
    merged: dict[int, dict] = {}
    for link in links:
        cfg = dict(link.config or {})
        prev = merged.get(int(link.panel_id))
        if prev is None:
            merged[int(link.panel_id)] = cfg
        else:
            merged[int(link.panel_id)] = orchestrator.merge_group_panel_configs(prev, cfg)

    return [(p, dict(merged.get(int(p.id)) or {}), 0) for p in panels]


async def _wipe_order_sample_on_remotes(
    db: AsyncSession,
    user: User,
    panels: list[tuple[Panel, dict, int]],
) -> None:
    """Hard-delete previous order probe on remotes (incl. Rebecca), then wipe local rows."""
    existing = list(
        (
            await db.execute(
                select(PanelAccount)
                .where(PanelAccount.user_id == user.id)
                .options(selectinload(PanelAccount.panel))
            )
        )
        .scalars()
        .all()
    )
    by_panel: dict[int, PanelAccount] = {}
    for acc in existing:
        by_panel.setdefault(int(acc.panel_id), acc)

    panel_map: dict[int, Panel] = {int(p.id): p for p, _c, _g in panels}
    for acc in existing:
        if acc.panel is not None:
            panel_map[int(acc.panel_id)] = acc.panel

    async def _remote_delete(panel: Panel, account: PanelAccount | None) -> None:
        remote_name = (account.remote_username if account else None) or user.username
        adapter = None
        try:
            adapter = get_adapter(panel)
            await asyncio.wait_for(
                adapter.delete_user(
                    remote_name,
                    account.remote_id if account else None,
                ),
                timeout=25,
            )
        except Exception:  # noqa: BLE001
            pass
        if adapter is not None:
            try:
                await adapter.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass

    # Delete on every target panel + any orphan account panel from last run
    jobs: list[tuple[Panel, PanelAccount | None]] = []
    seen: set[int] = set()
    for panel, _c, _g in panels:
        pid = int(panel.id)
        if pid in seen:
            continue
        seen.add(pid)
        jobs.append((panel, by_panel.get(pid)))
    for pid, acc in by_panel.items():
        if pid in seen:
            continue
        panel = panel_map.get(pid)
        if panel is None:
            continue
        seen.add(pid)
        jobs.append((panel, acc))

    if jobs:
        await asyncio.gather(*[_remote_delete(p, a) for p, a in jobs])

    for acc in existing:
        await db.delete(acc)
    await db.flush()


def _ingest_link(
    out: dict[tuple[str, int, str], dict[str, Any]],
    link: str,
    *,
    panel_name: str = "",
    group_name: str = "",
    label: str | None = None,
    protocol: str | None = None,
) -> None:
    raw = (link or "").strip()
    if not raw:
        return
    ep = extract_link_endpoint(raw)
    if not ep:
        return
    host, port = ep
    match = extract_link_match_host(raw) or ""
    norm = try_normalize_endpoint(host, port, match)
    if not norm:
        return
    host, port, match = norm
    key = _fp_key(host, port, match)
    proto, remark, _ = _parse_link_meta(raw)
    if key not in out:
        out[key] = {
            "host": host,
            "port": port,
            "match_host": match,
            "label": (label or remark or "").strip() or None,
            "sample_link": raw,
            "protocol": protocol or proto or None,
            "panel_names": [],
            "group_names": [],
        }
    entry = out[key]
    if panel_name and panel_name not in entry["panel_names"]:
        entry["panel_names"].append(panel_name)
    if group_name and group_name not in entry["group_names"]:
        entry["group_names"].append(group_name)
    if not entry.get("label") and (label or remark):
        entry["label"] = (label or remark or "").strip() or None
    if not entry.get("sample_link"):
        entry["sample_link"] = raw
    if not entry.get("protocol") and (protocol or proto):
        entry["protocol"] = protocol or proto


def _endpoint_meta(host: str, port: int, match_host: str) -> dict[str, Any]:
    """Hide + flag fields for one fingerprint (same rules as subscription)."""
    from app.services.config_hides import all_hidden_endpoints
    from app.services.config_locations import resolve_location_for_endpoint

    match = match_host or ""
    hidden = (host, int(port), match) in all_hidden_endpoints()
    loc = resolve_location_for_endpoint(host, int(port), match)
    if loc:
        return {
            "hidden": hidden,
            "country_code": str(loc.get("country_code") or "") or None,
            "flag": str(loc.get("flag") or "") or None,
            "flag_exact": bool(loc.get("exact")),
        }
    return {
        "hidden": hidden,
        "country_code": None,
        "flag": None,
        "flag_exact": False,
    }


def _item_out(
    *,
    host: str,
    port: int,
    match_host: str,
    label: str | None,
    sort_order: int,
    in_sample: bool,
    sample: dict[str, Any] | None,
) -> ConfigOrderItemOut:
    meta = _endpoint_meta(host, port, match_host)
    s = sample or {}
    return ConfigOrderItemOut(
        host=host,
        port=port,
        match_host=match_host,
        label=label,
        sort_order=sort_order,
        in_sample=in_sample,
        sample_link=s.get("sample_link"),
        protocol=s.get("protocol"),
        panel_names=list(s.get("panel_names") or []),
        group_names=list(s.get("group_names") or []),
        hidden=bool(meta["hidden"]),
        country_code=meta["country_code"],
        flag=meta["flag"],
        flag_exact=bool(meta["flag_exact"]),
    )


async def _sample_fingerprints(db: AsyncSession) -> dict[tuple[str, int, str], dict[str, Any]]:
    """Unique endpoints from order-sample on ALL panels (+ any leftover group samples)."""
    from app.models.group_config import GroupConfigItem

    out: dict[tuple[str, int, str], dict[str, Any]] = {}
    user = await _get_order_sample_user(db)
    if user is not None:
        for account in user.panel_accounts or []:
            status_val = getattr(account.status, "value", account.status)
            if status_val not in (PanelAccountStatus.active.value, "active"):
                continue
            panel_name = ""
            if account.panel is not None:
                panel_name = str(getattr(account.panel, "name", "") or "")
            for link in account.links or []:
                if isinstance(link, str) and is_share_link(link):
                    _ingest_link(out, link, panel_name=panel_name)

    # Supplement so nothing is missing if a panel only appeared in group samples
    result = await db.execute(
        select(GroupConfigItem)
        .options(
            selectinload(GroupConfigItem.panel),
            selectinload(GroupConfigItem.group),
        )
        .order_by(GroupConfigItem.sort_order, GroupConfigItem.id)
    )
    for item in result.scalars().all():
        link = (item.sample_link or "").strip()
        if not link or not is_share_link(link):
            continue
        panel_name = ""
        if item.panel is not None:
            panel_name = str(getattr(item.panel, "name", "") or "")
        group_name = ""
        if item.group is not None:
            group_name = str(getattr(item.group, "name", "") or "")
        _ingest_link(
            out,
            link,
            panel_name=panel_name,
            group_name=group_name,
            label=(item.label or None),
            protocol=(item.protocol or None),
        )
    return out


async def list_workspace(db: AsyncSession) -> ConfigOrderWorkspaceOut:
    samples = await _sample_fingerprints(db)
    result = await db.execute(
        select(ConfigLinkOrder).order_by(
            ConfigLinkOrder.sort_order.asc(), ConfigLinkOrder.id.asc()
        )
    )
    saved = list(result.scalars().all())

    items: list[ConfigOrderItemOut] = []
    seen: set[tuple[str, int, str]] = set()

    # 1) Saved order first (admin arrangement)
    for i, row in enumerate(saved):
        try:
            host = normalize_host(row.host)
            port = int(row.port)
            match = normalize_match_host(row.match_host or "")
        except (ValueError, TypeError):
            continue
        key = _fp_key(host, port, match)
        sample = samples.get(key)
        items.append(
            _item_out(
                host=host,
                port=port,
                match_host=match,
                label=row.label or (sample or {}).get("label"),
                sort_order=i,
                in_sample=sample is not None,
                sample=sample,
            )
        )
        seen.add(key)

    # 2) New sample endpoints not yet ordered — append
    for key, sample in samples.items():
        if key in seen:
            continue
        items.append(
            _item_out(
                host=sample["host"],
                port=sample["port"],
                match_host=sample["match_host"],
                label=sample.get("label"),
                sort_order=len(items),
                in_sample=True,
                sample=sample,
            )
        )
        seen.add(key)

    return ConfigOrderWorkspaceOut(
        items=items,
        sample_ready=bool(samples),
        total_sample_links=len(samples),
        ordered_count=len(saved),
    )


async def save_order(db: AsyncSession, items: list[ConfigOrderItemIn]) -> ConfigOrderWorkspaceOut:
    """Replace the full global order list.

    Invalid fingerprints are skipped (never abort the whole reorder) so one
    odd host cannot block saving the rest — order still applies to all subs.
    """
    await db.execute(delete(ConfigLinkOrder))
    await db.flush()

    seen: set[tuple[str, int, str]] = set()
    saved = 0
    skipped = 0
    for i, raw in enumerate(items):
        norm = try_normalize_endpoint(raw.host, int(raw.port), raw.match_host or "")
        if not norm:
            skipped += 1
            logger.warning(
                "skip invalid order item host=%r port=%r match=%r",
                raw.host,
                raw.port,
                raw.match_host,
            )
            continue
        host, port, match = norm
        key = _fp_key(host, port, match)
        if key in seen:
            continue
        seen.add(key)
        db.add(
            ConfigLinkOrder(
                host=host,
                port=port,
                match_host=match,
                sort_order=saved,
                label=_normalize_label(raw.label),
            )
        )
        saved += 1
    if saved == 0 and items:
        raise ValueError("هیچ دامنه معتبری برای ذخیره ترتیب نبود")
    await db.commit()
    await refresh_cache(db)
    if skipped:
        logger.info("config order saved=%s skipped=%s", saved, skipped)
    return await list_workspace(db)


async def reconcile_order_with_sample(db: AsyncSession) -> int:
    """Drop stale order rows after rebuild; keep surviving order; append new endpoints."""
    samples = await _sample_fingerprints(db)
    result = await db.execute(
        select(ConfigLinkOrder).order_by(
            ConfigLinkOrder.sort_order.asc(), ConfigLinkOrder.id.asc()
        )
    )
    saved = list(result.scalars().all())
    await db.execute(delete(ConfigLinkOrder))
    await db.flush()

    seen: set[tuple[str, int, str]] = set()
    order_i = 0
    for row in saved:
        try:
            host = normalize_host(row.host)
            port = int(row.port)
            match = normalize_match_host(row.match_host or "")
        except (ValueError, TypeError):
            continue
        key = _fp_key(host, port, match)
        if key not in samples or key in seen:
            continue
        seen.add(key)
        sample = samples[key]
        db.add(
            ConfigLinkOrder(
                host=host,
                port=port,
                match_host=match,
                sort_order=order_i,
                label=_normalize_label(row.label or sample.get("label")),
            )
        )
        order_i += 1

    for key, sample in samples.items():
        if key in seen:
            continue
        seen.add(key)
        db.add(
            ConfigLinkOrder(
                host=sample["host"],
                port=int(sample["port"]),
                match_host=sample.get("match_host") or "",
                sort_order=order_i,
                label=_normalize_label(sample.get("label")),
            )
        )
        order_i += 1

    await db.commit()
    await refresh_cache(db)
    return order_i


async def create_or_refresh_samples(db: AsyncSession) -> dict:
    """Build order probe on every active panel: wipe previous remotes, then recreate."""
    panels = await _resolve_all_active_panels(db)
    if not panels:
        raise ValueError("هیچ پنل فعالی ثبت نشده است")

    user = await _ensure_order_sample_user(db)
    # Always remove previous test accounts on remotes before rebuild
    await _wipe_order_sample_on_remotes(db, user, panels)
    await asyncio.sleep(0.5)

    user = await _get_order_sample_user(db) or user

    # Rebecca soft-deletes usernames — each rebuild needs a fresh remote name
    rebecca_targets = [
        (p, c, g) for p, c, g in panels if getattr(p, "type", None) == PanelType.rebecca
    ]
    if rebecca_targets:
        for panel, _cfg, _gid in rebecca_targets:
            fresh = f"astra_o{secrets.token_hex(5)}"[:32]
            db.add(
                PanelAccount(
                    user_id=user.id,
                    panel_id=panel.id,
                    group_id=None,
                    status=PanelAccountStatus.pending,
                    remote_username=fresh,
                )
            )
        await db.flush()

    await orchestrator.provision_user_on_panels(db, user, panels)

    # If Rebecca still trapped (soft-delete), rotate remote name once more
    user = await _get_order_sample_user(db) or user
    trapped_rebecca: list[tuple[Panel, dict, int]] = []
    for panel, cfg, gid in rebecca_targets:
        acc = next(
            (
                a
                for a in (user.panel_accounts or [])
                if int(a.panel_id) == int(panel.id)
            ),
            None,
        )
        err = (acc.error_message or "").lower() if acc else ""
        if acc is None or acc.status != PanelAccountStatus.active or (
            "soft-delete" in err
            or "user not found" in err
            or "already exists" in err
        ):
            if acc is not None:
                acc.remote_username = f"astra_o{secrets.token_hex(5)}"[:32]
                acc.status = PanelAccountStatus.pending
                acc.error_message = None
            trapped_rebecca.append((panel, cfg, gid))
    if trapped_rebecca:
        await db.flush()
        await orchestrator.provision_user_on_panels(db, user, trapped_rebecca)

    # Expand /sub → real share links (do not treat subscription URLs as success)
    for attempt in range(4):
        user = await _get_order_sample_user(db)
        if not user:
            break
        try:
            await asyncio.wait_for(
                orchestrator.hydrate_share_links(db, user),
                timeout=18.0,
            )
        except Exception:  # noqa: BLE001
            pass
        user = await _get_order_sample_user(db)
        if not user:
            break
        accounts = list(user.panel_accounts or [])
        active = [
            a
            for a in accounts
            if getattr(a.status, "value", a.status)
            in (PanelAccountStatus.active.value, "active")
        ]

        def _shares(acc: PanelAccount) -> list[str]:
            return [
                str(x).strip()
                for x in (acc.links or [])
                if x and is_share_link(str(x))
            ]

        share_total = sum(len(_shares(a)) for a in active)
        still_need = any(
            (not _shares(a))
            and (
                not (a.links or [])
                or any(is_subscription_ref(str(x)) for x in (a.links or []) if x)
            )
            for a in active
        )
        if active and share_total > 0 and not still_need:
            break
        if attempt < 3:
            await asyncio.sleep(0.45 + 0.25 * attempt)

    await db.commit()
    # Replace stale fingerprints in saved order with the fresh sample set
    ordered_n = await reconcile_order_with_sample(db)

    user = await _get_order_sample_user(db)
    rows: list[dict[str, Any]] = []
    ok_count = 0
    fail_count = 0
    link_count = 0
    accounts_by_panel: dict[int, PanelAccount] = {}
    if user is not None:
        for acc in user.panel_accounts or []:
            accounts_by_panel[int(acc.panel_id)] = acc

    for panel, _cfg, _gid in panels:
        acc = accounts_by_panel.get(int(panel.id))
        n_links = (
            sum(1 for x in (acc.links or []) if x and is_share_link(str(x)))
            if acc
            else 0
        )
        status_val = getattr(acc.status, "value", acc.status) if acc else None
        is_active = status_val == PanelAccountStatus.active.value or status_val == "active"
        ok = bool(acc and is_active and n_links > 0)
        if ok:
            ok_count += 1
            link_count += n_links
        else:
            fail_count += 1
        rows.append(
            {
                "panel_id": int(panel.id),
                "panel_name": panel.name,
                "ok": ok,
                "links_count": n_links,
                "error": None
                if ok
                else (
                    (acc.error_message if acc else None)
                    or ("بدون لینک" if acc and is_active else "ساخت ناموفق")
                ),
            }
        )

    return {
        "ok_count": ok_count,
        "fail_count": fail_count,
        "panels_count": len(panels),
        "links_count": link_count,
        "ordered_count": ordered_n,
        "sample_username": ORDER_SAMPLE_USERNAME,
        "panels": rows,
    }
