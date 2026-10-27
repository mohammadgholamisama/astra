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
from app.models.group import Group, UserGroup
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
        key = _fp_key(host, port, match)
        # Keep earliest sort_order when www./non-www collapse to the same key
        if key in next_map:
            continue
        next_map[key] = i
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
    """Sort rank for a share link; large number if not in the global list.

    Match priority:
      1) exact host:port:match_host (www. already normalized away)
      2) host:port with empty match_host (general row)
    Never collapse different SNIs on the same host:port — those are distinct slots.
    """
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
        exact = _order_idx.get(_fp_key(host, port, match))
        if exact is not None:
            return exact
        if match:
            general = _order_idx.get(_fp_key(host, port, ""))
            if general is not None:
                return general
        return 10**6


def _normalize_label(raw: str | None) -> str | None:
    text = " ".join(str(raw or "").split()).strip()
    if not text:
        return None
    return text[:128]


async def _get_order_sample_user(db: AsyncSession) -> User | None:
    result = await db.execute(
        select(User)
        .where(User.username == ORDER_SAMPLE_USERNAME)
        .options(
            selectinload(User.groups),
            selectinload(User.panel_accounts).selectinload(PanelAccount.panel),
        )
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


async def _all_enabled_group_ids(db: AsyncSession) -> list[int]:
    result = await db.execute(
        select(Group.id)
        .where(Group.is_disabled.is_(False))
        .order_by(Group.id.asc())
    )
    return [int(gid) for gid in result.scalars().all()]


async def _resolve_order_sample_targets(
    db: AsyncSession,
) -> list[tuple[Panel, dict, int]]:
    """Same targets as creating a user with every enabled sales group.

    One (panel, config, group_id) per GroupPanel — not a merged one-account-per-panel
    probe (that inflated fingerprints vs real multi-group users).
    """
    gids = await _all_enabled_group_ids(db)
    if not gids:
        return []
    return await orchestrator.resolve_panels_for_groups(db, gids)


async def _sync_order_sample_groups(
    db: AsyncSession, user: User, group_ids: list[int]
) -> None:
    """Keep order-sample membership identical to an all-groups user."""
    await db.execute(delete(UserGroup).where(UserGroup.user_id == user.id))
    for i, gid in enumerate(group_ids):
        db.add(UserGroup(user_id=user.id, group_id=int(gid), sort_order=i))
    await db.flush()


async def _wipe_order_sample_on_remotes(
    db: AsyncSession,
    user: User,
    targets: list[tuple[Panel, dict, int]],
) -> None:
    """Hard-delete every previous order-probe remote (panel×group), then wipe local rows."""
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

    panel_map: dict[int, Panel] = {int(p.id): p for p, _c, _g in targets}
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

    # One remote DELETE per local account (unique remote_username when multi-group)
    jobs: list[tuple[Panel, PanelAccount | None]] = []
    seen_names: set[tuple[int, str]] = set()
    for acc in existing:
        panel = panel_map.get(int(acc.panel_id))
        if panel is None:
            continue
        rname = (acc.remote_username or user.username or "").strip()
        key = (int(panel.id), rname)
        if key in seen_names:
            continue
        seen_names.add(key)
        jobs.append((panel, acc))

    # Also try base username once per target panel (legacy merged probe)
    for panel, _c, _g in targets:
        key = (int(panel.id), user.username)
        if key in seen_names:
            continue
        seen_names.add(key)
        jobs.append((panel, None))

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
    avg_ping: float | None = None,
    device_count: int | None = None,
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
        avg_ping=avg_ping,
        device_count=device_count,
    )


async def _sample_fingerprints(db: AsyncSession) -> dict[tuple[str, int, str], dict[str, Any]]:
    """Unique endpoints from the order-sample user — same set a full-group sub would emit.

    Uses ``collect_links_detailed`` (panel×group membership + share-link rules) then
    fingerprint-dedupes like the live subscription. Does not pull stale GroupConfigItem
    leftovers (those inflated the order page vs real users).
    """
    out: dict[tuple[str, int, str], dict[str, Any]] = {}
    result = await db.execute(
        select(User)
        .where(User.username == ORDER_SAMPLE_USERNAME)
        .options(
            selectinload(User.groups),
            selectinload(User.panel_accounts).selectinload(PanelAccount.panel),
        )
        .execution_options(populate_existing=True)
    )
    user = result.scalar_one_or_none()
    if user is None:
        return out

    try:
        rows = await orchestrator.collect_links_detailed(
            user,
            include_notices=False,
            include_hidden=True,
            db=db,
        )
    except Exception:  # noqa: BLE001
        logger.exception("order-sample collect_links failed; falling back to raw accounts")
        rows = []
        for account in user.panel_accounts or []:
            status_val = getattr(account.status, "value", account.status)
            if status_val not in (
                PanelAccountStatus.active.value,
                "active",
                PanelAccountStatus.error.value,
                "error",
            ):
                continue
            panel_name = ""
            if account.panel is not None:
                panel_name = str(getattr(account.panel, "name", "") or "")
            for link in account.links or []:
                if isinstance(link, str) and is_share_link(link):
                    rows.append({"link": link, "panel": panel_name})

    # Mirror subscription fingerprint collapse (host:port[+SNI][+path])
    from app.services.config_locations import extract_link_path

    seen_fp: set[tuple] = set()
    for row in rows:
        link = str(row.get("link") or "").strip()
        if not link or not is_share_link(link):
            continue
        ep = extract_link_endpoint(link)
        if not ep:
            continue
        match = extract_link_match_host(link) or ""
        norm = try_normalize_endpoint(ep[0], ep[1], match)
        if not norm:
            continue
        key: tuple = (*norm, extract_link_path(link))
        if key in seen_fp:
            continue
        seen_fp.add(key)
        panel_name = str(row.get("panel") or "")
        group_name = ""
        gid = row.get("group_id")
        if gid is not None:
            for g in user.groups or []:
                if int(g.id) == int(gid):
                    group_name = str(g.name or "")
                    break
        _ingest_link(
            out,
            link,
            panel_name=panel_name,
            group_name=group_name,
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
        if key in seen:
            # www./non-www (or other normalize collapses) — keep earliest row
            continue
        sample = samples.get(key)
        avg = getattr(row, "avg_ping", None)
        dcount = getattr(row, "device_count", None)
        try:
            avg_f = float(avg) if avg is not None else None
        except (TypeError, ValueError):
            avg_f = None
        try:
            dcount_i = int(dcount) if dcount is not None else None
        except (TypeError, ValueError):
            dcount_i = None
        items.append(
            _item_out(
                host=host,
                port=port,
                match_host=match,
                label=row.label or (sample or {}).get("label"),
                sort_order=len(items),
                in_sample=sample is not None,
                sample=sample,
                avg_ping=avg_f,
                device_count=dcount_i,
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


async def apply_ping_rank(
    db: AsyncSession,
    ranked: list[dict[str, Any]],
) -> ConfigOrderWorkspaceOut:
    """Reorder config-order + auto hide/unhide from multi-device pings.

    Order rules:
    - Up to 5 best مستقیم, then 5 تانل, then 5 گیمینگ (by device_count then ping)
    - Remaining successful fingerprints by quality
    - Untested / unmatched keep relative order after scored ones

    Sub visibility:
    - All reporting devices failed → hide from every user subscription
    - Any device succeeded → unhide (re-enter sub)
    - Priority for ranking: more successful devices first
    """
    from app.services.config_hides import set_hidden
    from app.services.ping_category_order import arrange_by_category_slots

    workspace = await list_workspace(db)
    current = list(workspace.items)

    score: dict[tuple[str, int, str], float] = {}
    devices: dict[tuple[str, int, str], int] = {}
    fails: dict[tuple[str, int, str], int] = {}
    labels: dict[tuple[str, int, str], str] = {}
    tested_fail: set[tuple[str, int, str]] = set()

    for raw in ranked:
        try:
            host = str(raw.get("host") or "")
            port = int(raw.get("port") or 0)
            match = str(raw.get("match_host") or "")
            avg = float(raw.get("avg_ping") if raw.get("avg_ping") is not None else -1)
            dcount = int(raw.get("device_count") or 0)
            fcount = int(raw.get("fail_device_count") or 0)
            samples = int(raw.get("sample_count") or 0)
        except (TypeError, ValueError):
            continue
        norm = try_normalize_endpoint(host, port, match)
        if not norm:
            continue
        key = _fp_key(*norm)
        label = _normalize_label(raw.get("label"))
        if label and key not in labels:
            labels[key] = label

        if avg > 0:
            if key not in score or avg < score[key]:
                score[key] = avg
                devices[key] = max(dcount, 1)
            elif dcount > devices.get(key, 0):
                devices[key] = dcount
            # success always clears hide candidate
            tested_fail.discard(key)
        else:
            # Tested but no positive device → candidate for hide
            if samples > 0 or fcount > 0 or dcount == 0:
                if key not in score:
                    tested_fail.add(key)
                    fails[key] = max(fcount, fails.get(key, 0), 1)

    # Auto hide / unhide for subscription feeds
    hide_n = 0
    show_n = 0
    for it in current:
        key = _fp_key(it.host, int(it.port), it.match_host or "")
        try:
            if key in score:
                await set_hidden(
                    db,
                    None,
                    it.host,
                    int(it.port),
                    hidden=False,
                    match_host=it.match_host or "",
                    commit=False,
                )
                show_n += 1
            elif key in tested_fail:
                await set_hidden(
                    db,
                    None,
                    it.host,
                    int(it.port),
                    hidden=True,
                    match_host=it.match_host or "",
                    commit=False,
                )
                hide_n += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("apply_ping_rank hide/unhide %s: %s", key, exc)

    if not score:
        await db.commit()
        from app.services.config_hides import refresh_cache as refresh_hides

        await refresh_hides(db)
        logger.info(
            "apply_ping_rank no scores — hide=%s show=%s",
            hide_n,
            show_n,
        )
        return await list_workspace(db)

    scored_items: list[ConfigOrderItemOut] = []
    unscored_rows: list[ConfigOrderItemOut] = []
    for it in current:
        key = _fp_key(it.host, int(it.port), it.match_host or "")
        if key in score:
            scored_items.append(it)
        else:
            unscored_rows.append(it)

    if not scored_items:
        await db.commit()
        from app.services.config_hides import refresh_cache as refresh_hides

        await refresh_hides(db)
        logger.info(
            "apply_ping_rank skip order — %s scores, 0 panel matches hide=%s",
            len(score),
            hide_n,
        )
        return await list_workspace(db)

    arranged = arrange_by_category_slots(
        scored_items,
        avg_of=lambda it: score[_fp_key(it.host, int(it.port), it.match_host or "")],
        label_of=lambda it: it.label
        or labels.get(_fp_key(it.host, int(it.port), it.match_host or ""), "")
        or "",
        device_of=lambda it: devices.get(
            _fp_key(it.host, int(it.port), it.match_host or ""), 1
        ),
    )
    ordered = list(arranged) + unscored_rows

    payload: list[ConfigOrderItemIn] = []
    for it in ordered:
        key = _fp_key(it.host, int(it.port), it.match_host or "")
        label = it.label or labels.get(key)
        payload.append(
            ConfigOrderItemIn(
                host=it.host,
                port=int(it.port),
                match_host=it.match_host or "",
                label=label,
            )
        )

    logger.info(
        "apply_ping_rank scored=%s unscored=%s hide=%s show=%s total=%s",
        len(arranged),
        len(unscored_rows),
        hide_n,
        show_n,
        len(payload),
    )
    await save_order(db, payload)

    result = await db.execute(select(ConfigLinkOrder))
    rows = list(result.scalars().all())
    for row in rows:
        try:
            key = _fp_key(
                normalize_host(row.host),
                int(row.port),
                normalize_match_host(row.match_host or ""),
            )
        except (ValueError, TypeError):
            continue
        if key in score:
            row.avg_ping = float(score[key])
            row.device_count = int(devices.get(key) or 1)
    await db.commit()
    await refresh_cache(db)
    from app.services.config_hides import refresh_cache as refresh_hides

    await refresh_hides(db)
    return await list_workspace(db)


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
    """Rebuild order probe exactly like creating a user on every enabled group."""
    group_ids = await _all_enabled_group_ids(db)
    targets = await _resolve_order_sample_targets(db)
    if not targets:
        raise ValueError("هیچ پنل فعالی در گروه‌های فروش ثبت نشده است")

    user = await _ensure_order_sample_user(db)
    await _sync_order_sample_groups(db, user, group_ids)
    # Always remove previous test accounts on remotes before rebuild
    await _wipe_order_sample_on_remotes(db, user, targets)
    await asyncio.sleep(0.5)

    user = await _get_order_sample_user(db) or user
    await _sync_order_sample_groups(db, user, group_ids)

    # Rebecca soft-deletes usernames — pre-seed fresh remote names per (panel×group)
    rebecca_targets = [
        (p, c, g)
        for p, c, g in targets
        if getattr(p, "type", None) == PanelType.rebecca
    ]
    if rebecca_targets:
        for panel, _cfg, gid in rebecca_targets:
            fresh = f"astra_o{secrets.token_hex(5)}"[:32]
            db.add(
                PanelAccount(
                    user_id=user.id,
                    panel_id=panel.id,
                    group_id=int(gid) if gid else None,
                    status=PanelAccountStatus.pending,
                    remote_username=fresh,
                )
            )
        await db.flush()

    await orchestrator.provision_user_on_panels(db, user, targets)

    # If Rebecca still trapped (soft-delete), rotate remote name once more
    user = await _get_order_sample_user(db) or user
    trapped_rebecca: list[tuple[Panel, dict, int]] = []
    for panel, cfg, gid in rebecca_targets:
        gid_i = int(gid) if gid else 0
        acc = next(
            (
                a
                for a in (user.panel_accounts or [])
                if int(a.panel_id) == int(panel.id)
                and int(getattr(a, "group_id", 0) or 0) == gid_i
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
    fps = await _sample_fingerprints(db)
    link_count = len(fps)

    # Per unique panel status for the toast (aggregate panel×group accounts)
    rows: list[dict[str, Any]] = []
    ok_count = 0
    fail_count = 0
    by_panel: dict[int, list[PanelAccount]] = {}
    if user is not None:
        for acc in user.panel_accounts or []:
            by_panel.setdefault(int(acc.panel_id), []).append(acc)

    seen_panels: set[int] = set()
    panel_rows: list[tuple[Panel, list[PanelAccount]]] = []
    for panel, _cfg, _gid in targets:
        pid = int(panel.id)
        if pid in seen_panels:
            continue
        seen_panels.add(pid)
        panel_rows.append((panel, by_panel.get(pid) or []))

    for panel, accs in panel_rows:
        n_links = sum(
            sum(1 for x in (a.links or []) if x and is_share_link(str(x)))
            for a in accs
        )
        any_active = any(
            getattr(a.status, "value", a.status)
            in (PanelAccountStatus.active.value, "active")
            for a in accs
        )
        ok = bool(accs and any_active and n_links > 0)
        if ok:
            ok_count += 1
        else:
            fail_count += 1
        err = None
        if not ok:
            err = next(
                (a.error_message for a in accs if a.error_message),
                "بدون لینک" if accs and any_active else "ساخت ناموفق",
            )
        rows.append(
            {
                "panel_id": int(panel.id),
                "panel_name": panel.name,
                "ok": ok,
                "links_count": n_links,
                "accounts": len(accs),
                "error": err,
            }
        )

    return {
        "ok_count": ok_count,
        "fail_count": fail_count,
        "panels_count": len(panel_rows),
        "targets_count": len(targets),
        "groups_count": len(group_ids),
        "links_count": link_count,
        "ordered_count": ordered_n,
        "sample_username": ORDER_SAMPLE_USERNAME,
        "panels": rows,
    }
