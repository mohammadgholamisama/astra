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
    extract_link_path,
    extract_link_remark_key,
    match_host_sni_only,
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


def _sni_core(match_host: str) -> str:
    """Strip Astra ``|ob|remark`` suffix so order ↔ sample fingerprints align."""
    try:
        return normalize_match_host(match_host_sni_only(match_host or ""))
    except ValueError:
        return (match_host_sni_only(match_host or "") or "").strip().lower()


def _find_sample(
    samples: dict[tuple[str, int, str], dict[str, Any]],
    host: str,
    port: int,
    match_host: str = "",
) -> dict[str, Any] | None:
    """Resolve a sample row for a saved order fingerprint (exact, then fuzzy).

    Saved order often stores plain SNI while the live sample uses ``sni|ob|name``
    (Astra outbounds). Exact-only lookup falsely marks hundreds as «not in sample»
    and reconcile then drops them — breaking sub numbering / visibility.
    """
    if not samples:
        return None
    key = _fp_key(host, int(port), match_host or "")
    hit = samples.get(key)
    if hit is not None:
        return hit
    core = _sni_core(match_host or "")
    empty = _fp_key(host, int(port), "")
    if empty in samples:
        return samples[empty]
    # Prefer sample whose SNI core matches (with or without |ob|remark)
    candidates: list[dict[str, Any]] = []
    for sk, sample in samples.items():
        if sk[0] != host or int(sk[1]) != int(port):
            continue
        if _sni_core(sk[2] or "") == core:
            candidates.append(sample)
    if candidates:
        # Prefer the |ob| slot when several share the same SNI
        for sample in candidates:
            if "|ob|" in str(sample.get("match_host") or ""):
                return sample
        return candidates[0]
    # Last resort: any sample on same host:port
    for sk, sample in samples.items():
        if sk[0] == host and int(sk[1]) == int(port):
            return sample
    return None


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
      1) Astra outbound slot host:port:sni|ob|remark
      2) exact host:port:match_host
      3) host:port with empty match_host
    """
    ep = extract_link_endpoint(link)
    if not ep:
        return 10**6
    host, port = ep
    match = extract_link_match_host(link) or ""
    remark = extract_link_remark_key(link)
    norm = try_normalize_endpoint(host, port, match)
    if not norm:
        return 10**6
    host, port, match = norm
    with _lock:
        if remark:
            ob_key = _fp_key(
                host, port, f"{match}|ob|{remark}" if match else f"|ob|{remark}"
            )
            hit = _order_idx.get(ob_key)
            if hit is not None:
                return hit
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
                timeout=1.5,
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
    distinct_remark: bool = False,
    category_name: str | None = None,
    usage_cost: int | None = None,
) -> None:
    raw = (link or "").strip()
    if not raw:
        return
    ep = extract_link_endpoint(raw)
    if not ep:
        return
    host, port = ep
    match = extract_link_match_host(raw) or ""
    remark_key = extract_link_remark_key(raw)
    if distinct_remark and remark_key:
        match = f"{match}|ob|{remark_key}" if match else f"|ob|{remark_key}"
    norm = try_normalize_endpoint(host, port, match)
    if not norm:
        return
    host, port, match = norm
    key = _fp_key(host, port, match)
    proto, remark, _ = _parse_link_meta(raw)
    panel_remark = (remark or "").strip() or None
    if key not in out:
        out[key] = {
            "host": host,
            "port": port,
            "match_host": match,
            "label": (label or "").strip() or None,
            "source_remark": panel_remark,
            "sample_link": raw,
            "protocol": protocol or proto or None,
            "panel_names": [],
            "group_names": [],
            "category_name": None,
            "usage_cost": None,
        }
    entry = out[key]
    if panel_name and panel_name not in entry["panel_names"]:
        entry["panel_names"].append(panel_name)
    if group_name and group_name not in entry["group_names"]:
        entry["group_names"].append(group_name)
    if label:
        entry["label"] = str(label).strip() or entry.get("label")
    elif not entry.get("label") and remark:
        entry["label"] = remark.strip() or None
    if panel_remark and not entry.get("source_remark"):
        entry["source_remark"] = panel_remark
    if not entry.get("sample_link"):
        entry["sample_link"] = raw
    if not entry.get("protocol") and (protocol or proto):
        entry["protocol"] = protocol or proto
    cat = (category_name or "").strip() or None
    if cat and not entry.get("category_name"):
        entry["category_name"] = cat
    if usage_cost is not None:
        try:
            cost_i = int(usage_cost)
        except (TypeError, ValueError):
            cost_i = None
        if cost_i is not None and cost_i >= 0:
            prev = entry.get("usage_cost")
            if prev is None or cost_i < int(prev):
                entry["usage_cost"] = cost_i


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
    source_remark = (str(s.get("source_remark") or "").strip() or None)
    # If sample has no stored remark, fall back to parsing the share link
    if not source_remark:
        raw_link = str(s.get("sample_link") or "").strip()
        if raw_link:
            try:
                _p, rem, _ = _parse_link_meta(raw_link)
                source_remark = (rem or "").strip() or None
            except Exception:  # noqa: BLE001
                source_remark = None
    display = (label or "").strip() or None
    # Prefer showing the real panel remark as the primary title when label is
    # only a group tag / empty — keep source_remark for the secondary line.
    if not display and source_remark:
        display = source_remark
    cost_raw = s.get("usage_cost")
    try:
        cost_out = int(cost_raw) if cost_raw is not None else None
    except (TypeError, ValueError):
        cost_out = None
    cat_out = (str(s.get("category_name") or "").strip() or None)
    return ConfigOrderItemOut(
        host=host,
        port=port,
        match_host=match_host,
        label=display,
        source_remark=source_remark,
        sort_order=sort_order,
        in_sample=in_sample,
        sample_link=s.get("sample_link"),
        protocol=s.get("protocol"),
        panel_names=list(s.get("panel_names") or []),
        group_names=list(s.get("group_names") or []),
        category_name=cat_out,
        usage_cost=cost_out,
        hidden=bool(meta["hidden"]),
        country_code=meta["country_code"],
        flag=meta["flag"],
        flag_exact=bool(meta["flag_exact"]),
        avg_ping=avg_ping,
        device_count=device_count,
    )


async def _sample_fingerprints(db: AsyncSession) -> dict[tuple[str, int, str], dict[str, Any]]:
    """Unique endpoints from RAW sample share links (every panel including Astra).

    Uses stored account.links, not collect_links_detailed — that rewrites remarks
    to ``#N. tag | username`` and would overwrite admin labels.
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

    groups_by_id = {int(g.id): g for g in (user.groups or []) if getattr(g, "id", None)}
    tag_by_gid: dict[int, str] = {}
    tag_by_panel: dict[int, str] = {}
    cost_by_gid: dict[int, int] = {}
    cat_by_gid: dict[int, str] = {}
    cost_by_panel: dict[int, int] = {}
    cat_by_panel: dict[int, str] = {}
    try:
        from app.models.group import Group, GroupPanel
        from app.services.ping_category_order import normalize_order_kind

        g_rows = await db.execute(
            select(
                Group.id,
                Group.config_tag,
                Group.name,
                Group.usage_cost,
                Group.order_kind,
            )
        )
        for gid, tag, gname, cost, order_kind in g_rows.all():
            if gid is None:
                continue
            gid_i = int(gid)
            t = _normalize_label(tag or "") or ""
            if t:
                tag_by_gid[gid_i] = t
            if gname and gid_i not in groups_by_id:
                # lightweight stand-in so account.group_id resolves name
                pass
            try:
                if cost is not None:
                    cost_by_gid[gid_i] = int(cost)
            except (TypeError, ValueError):
                pass
            cat_by_gid[gid_i] = normalize_order_kind(order_kind)

        gp_rows = await db.execute(
            select(GroupPanel.panel_id, GroupPanel.group_id)
        )
        for pid, gid in gp_rows.all():
            if pid is None or gid is None:
                continue
            pid_i, gid_i = int(pid), int(gid)
            t = tag_by_gid.get(gid_i, "")
            if t and pid_i not in tag_by_panel:
                tag_by_panel[pid_i] = t
            if gid_i in cost_by_gid and pid_i not in cost_by_panel:
                cost_by_panel[pid_i] = cost_by_gid[gid_i]
            if gid_i in cat_by_gid and pid_i not in cat_by_panel:
                cat_by_panel[pid_i] = cat_by_gid[gid_i]
    except Exception:  # noqa: BLE001
        logger.debug("order-sample group tags/cost load failed", exc_info=True)
    for account in user.panel_accounts or []:
        st = getattr(account.status, "value", account.status)
        if st in (PanelAccountStatus.deleted.value, "deleted"):
            continue
        panel = account.__dict__.get("panel")
        if panel is not None and not bool(getattr(panel, "is_active", True)):
            continue
        panel_name = str(getattr(panel, "name", "") or "") if panel is not None else ""
        gid = getattr(account, "group_id", None)
        group_name = ""
        group_tag = ""
        category_name: str | None = None
        usage_cost: int | None = None
        if gid is not None:
            gid_i = int(gid)
            if gid_i in groups_by_id:
                g = groups_by_id[gid_i]
                group_name = str(getattr(g, "name", "") or "")
            group_tag = tag_by_gid.get(gid_i, "") or ""
            if not group_tag and gid_i in groups_by_id:
                group_tag = (
                    _normalize_label(
                        getattr(groups_by_id[gid_i], "config_tag", None) or ""
                    )
                    or ""
                )
            category_name = cat_by_gid.get(gid_i)
            usage_cost = cost_by_gid.get(gid_i)
        if not group_tag and panel is not None:
            group_tag = tag_by_panel.get(int(panel.id), "") or ""
        if category_name is None and panel is not None:
            category_name = cat_by_panel.get(int(panel.id))
        if usage_cost is None and panel is not None:
            usage_cost = cost_by_panel.get(int(panel.id))
        is_astra = (
            panel is not None
            and getattr(getattr(panel, "type", None), "value", getattr(panel, "type", None))
            in ("astra", PanelType.astra)
        )
        for link in account.links or []:
            text = str(link or "").strip()
            if not text or not is_share_link(text):
                continue
            _ingest_link(
                out,
                text,
                panel_name=panel_name,
                group_name=group_name,
                label=group_tag or None,
                distinct_remark=bool(is_astra),
                category_name=category_name,
                usage_cost=usage_cost,
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
    # Sample keys already covered via fuzzy match of a saved row
    covered_sample_keys: set[tuple[str, int, str]] = set()

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
        sample = _find_sample(samples, host, port, match)
        # Prefer live sample fingerprint so UI / hide / sub stay aligned
        out_host, out_port, out_match = host, port, match
        if sample is not None:
            try:
                out_host = normalize_host(str(sample.get("host") or host))
                out_port = int(sample.get("port") or port)
                out_match = normalize_match_host(str(sample.get("match_host") or match))
            except (ValueError, TypeError):
                out_host, out_port, out_match = host, port, match
            sk = _fp_key(out_host, out_port, out_match)
            covered_sample_keys.add(sk)
            # Also mark exact sample dict key
            for sk2, s2 in samples.items():
                if s2 is sample:
                    covered_sample_keys.add(sk2)
                    break
        out_key = _fp_key(out_host, out_port, out_match)
        if out_key in seen:
            continue
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
                host=out_host,
                port=out_port,
                match_host=out_match,
                label=(sample or {}).get("label") or row.label,
                sort_order=len(items),
                in_sample=sample is not None,
                sample=sample,
                avg_ping=avg_f,
                device_count=dcount_i,
            )
        )
        seen.add(out_key)
        seen.add(key)

    # 2) New sample endpoints not yet ordered — append
    for key, sample in samples.items():
        if key in seen or key in covered_sample_keys:
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
    """Reorder config-order from multi-device pings — NEVER auto-hide.

    App / hub ping tests only change sort order. Subscription visibility is
    manual (config-order hide toggles) — failed ping must not remove from sub.

    One-shot: clear leftover auto-hides from older hub builds that wrongly
    hid dead pings (flag file under data/).

    Order rules:
    - Up to 20 مستقیم (usage_cost↑ then quality)
    - Tunnel tiers by usage_cost↑ — up to 35 per cost (e.g. ۵ سپس ۷)
    - Up to 9 گیمینگ (usage_cost↑ then quality)
    - Remaining by cost then quality
    Untested configs still fill their order_kind slot (sorted after scored peers).
    """
    from app.services.config_hides import clear_stale_ping_auto_hides_once
    from app.services.ping_category_order import arrange_by_category_slots

    # Never call set_hidden(True) here. One-shot undo of old auto-hide damage.
    await clear_stale_ping_auto_hides_once(db)

    workspace = await list_workspace(db)
    current = list(workspace.items)

    score: dict[tuple[str, int, str], float] = {}
    downloads: dict[tuple[str, int, str], float] = {}
    uploads: dict[tuple[str, int, str], float] = {}
    packet_loss: dict[tuple[str, int, str], float] = {}
    jitters: dict[tuple[str, int, str], float] = {}
    stabilities: dict[tuple[str, int, str], float] = {}
    uptimes: dict[tuple[str, int, str], float] = {}
    devices: dict[tuple[str, int, str], int] = {}
    labels: dict[tuple[str, int, str], str] = {}
    # host:port → best exact key (fallback when match_host differs app↔panel)
    by_host_port: dict[tuple[str, int], tuple[str, int, str]] = {}

    def _remember_hp(key: tuple[str, int, str]) -> None:
        hp = (key[0], key[1])
        prev = by_host_port.get(hp)
        if prev is None:
            by_host_port[hp] = key
            return
        # Prefer non-empty match; else keep first
        if not prev[2] and key[2]:
            by_host_port[hp] = key

    def _resolve_key(host: str, port: int, match: str) -> tuple[str, int, str] | None:
        exact = _fp_key(host, port, match or "")
        if exact in score:
            return exact
        empty = _fp_key(host, port, "")
        if empty in score:
            return empty
        return by_host_port.get((host, port))

    for raw in ranked:
        try:
            host = str(raw.get("host") or "")
            port = int(raw.get("port") or 0)
            match = str(raw.get("match_host") or "")
            avg = float(raw.get("avg_ping") if raw.get("avg_ping") is not None else -1)
            dl = float(raw.get("download_kbps") or 0)
            ul = float(raw.get("upload_kbps") or 0)
            has_pkt = bool(raw.get("has_packet_loss"))
            try:
                pkt = float(raw.get("packet_loss_pct")) if raw.get("packet_loss_pct") is not None else -1.0
            except (TypeError, ValueError):
                pkt = -1.0
            if has_pkt and pkt < 0:
                pkt = 0.0
            if not has_pkt and pkt >= 0:
                has_pkt = True
            try:
                jit = float(raw.get("jitter_ms")) if raw.get("jitter_ms") is not None else -1.0
            except (TypeError, ValueError):
                jit = -1.0
            try:
                stab = (
                    float(raw.get("stability_pct"))
                    if raw.get("stability_pct") is not None
                    else -1.0
                )
            except (TypeError, ValueError):
                stab = -1.0
            try:
                up = (
                    float(raw.get("uptime_pct"))
                    if raw.get("uptime_pct") is not None
                    else -1.0
                )
            except (TypeError, ValueError):
                up = -1.0
            dcount = int(raw.get("device_count") or 0)
        except (TypeError, ValueError):
            continue
        norm = try_normalize_endpoint(host, port, match)
        if not norm:
            continue
        key = _fp_key(*norm)
        label = _normalize_label(raw.get("label"))
        if label and key not in labels:
            labels[key] = label

        speed_ok = dl > 0 or ul > 0
        packet_ok = has_pkt and 0 <= pkt < 100
        stab_ok = stab > 0
        up_ok = up > 0
        # Failures are ignored for ordering — never scored, never hidden
        if not (avg > 0 or speed_ok or packet_ok or stab_ok or up_ok):
            continue

        if avg > 0:
            if key not in score or avg < score[key]:
                score[key] = avg
                devices[key] = max(dcount, 1)
            elif dcount > devices.get(key, 0):
                devices[key] = dcount
        else:
            # Speed/packet-only success (no ping) — keep in score for ordering
            if key not in score:
                score[key] = 0.0
            devices[key] = max(devices.get(key, 0), max(dcount, 1))
        if dl > 0 and dl > downloads.get(key, 0):
            downloads[key] = dl
        if ul > 0 and ul > uploads.get(key, 0):
            uploads[key] = ul
        if has_pkt and 0 <= pkt <= 100:
            prev_pkt = packet_loss.get(key)
            if prev_pkt is None or pkt < prev_pkt:
                packet_loss[key] = pkt
        if jit >= 0:
            prev_jit = jitters.get(key)
            if prev_jit is None or jit < prev_jit:
                jitters[key] = jit
        if stab >= 0:
            prev_stab = stabilities.get(key)
            if prev_stab is None or stab > prev_stab:
                stabilities[key] = stab
        if up >= 0:
            prev_up = uptimes.get(key)
            if prev_up is None or up > prev_up:
                uptimes[key] = up
        # Also index empty-match alias so panel rows without SNI still hit
        empty_key = _fp_key(key[0], key[1], "")
        if empty_key not in score:
            score[empty_key] = score[key]
            devices[empty_key] = devices.get(key, 1)
            if key in downloads:
                downloads[empty_key] = downloads[key]
            if key in uploads:
                uploads[empty_key] = uploads[key]
            if key in packet_loss:
                packet_loss[empty_key] = packet_loss[key]
            if key in jitters:
                jitters[empty_key] = jitters[key]
            if key in stabilities:
                stabilities[empty_key] = stabilities[key]
            if key in uptimes:
                uptimes[empty_key] = uptimes[key]
        _remember_hp(key)

    if not current:
        return await list_workspace(db)

    item_score_key: dict[int, tuple[str, int, str]] = {}
    for it in current:
        resolved = _resolve_key(it.host, int(it.port), it.match_host or "")
        if resolved is not None:
            item_score_key[id(it)] = resolved

    def _sk(it: ConfigOrderItemOut) -> tuple[str, int, str]:
        return item_score_key.get(id(it)) or _fp_key(
            it.host, int(it.port), it.match_host or ""
        )

    def _avg(it: ConfigOrderItemOut) -> float:
        key = _sk(it)
        if key in score:
            return float(score[key])
        try:
            existing = float(getattr(it, "avg_ping", None) or -1)
        except (TypeError, ValueError):
            existing = -1.0
        return existing if existing > 0 else 1e18

    # All fingerprints fill order_kind slots — ping only ranks inside a slot.
    arranged = arrange_by_category_slots(
        current,
        avg_of=_avg,
        label_of=lambda it: it.label
        or labels.get(_sk(it), "")
        or labels.get(_fp_key(it.host, int(it.port), it.match_host or ""), "")
        or "",
        device_of=lambda it: devices.get(_sk(it), getattr(it, "device_count", None) or 1),
        download_of=lambda it: downloads.get(_sk(it), 0),
        upload_of=lambda it: uploads.get(_sk(it), 0),
        packet_of=lambda it: packet_loss.get(_sk(it), 101.0),
        jitter_of=lambda it: jitters.get(_sk(it), -1.0),
        stability_of=lambda it: stabilities.get(_sk(it), -1.0),
        uptime_of=lambda it: uptimes.get(_sk(it), -1.0),
        # Only group.order_kind (exposed as category_name on workspace items)
        category_of=lambda it: getattr(it, "category_name", None) or "",
        cost_of=lambda it: getattr(it, "usage_cost", None),
    )
    ordered = list(arranged)

    payload: list[ConfigOrderItemIn] = []
    for it in ordered:
        key = _sk(it)
        label = it.label or labels.get(key) or labels.get(
            _fp_key(it.host, int(it.port), it.match_host or ""), None
        )
        payload.append(
            ConfigOrderItemIn(
                host=it.host,
                port=int(it.port),
                match_host=it.match_host or "",
                label=label,
            )
        )

    scored_n = sum(1 for it in current if _sk(it) in score)
    logger.info(
        "apply_ping_rank scored=%s total=%s (order_kind slots, no hide)",
        scored_n,
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
    workspace = await list_workspace(db)
    # Panel analytics: attribute this test cycle to each panel
    try:
        from app.services.panel_analytics import record_ping_rank_for_panels

        await record_ping_rank_for_panels(
            db, ranked, workspace_items=list(workspace.items)
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("panel test analytics after ping-rank failed")
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
    return workspace


async def seed_order_kind_from_folders_once(db: AsyncSession) -> int:
    """One-shot: copy folder name → group.order_kind when still default other.

    Only maps شناخته‌شده‌ها (مستقیم/تانل/گیمینگ). Manual order_kind wins afterward.
    Flag: ``data/seed_order_kind_v155``.
    """
    from pathlib import Path

    from app.models.group import Group, GroupCategory
    from app.services.ping_category_order import normalize_order_kind

    data_dir = Path(__file__).resolve().parents[3] / "data"
    flag = data_dir / "seed_order_kind_v155"
    if flag.exists():
        return 0
    try:
        cat_names: dict[int, str] = {}
        c_rows = await db.execute(select(GroupCategory.id, GroupCategory.name))
        for cid, cname in c_rows.all():
            if cid is not None and cname:
                cat_names[int(cid)] = str(cname).strip()
        result = await db.execute(select(Group))
        groups = list(result.scalars().all())
        n = 0
        for g in groups:
            current = normalize_order_kind(getattr(g, "order_kind", None) or "other")
            if current != "other":
                continue
            cid = getattr(g, "category_id", None)
            if cid is None:
                continue
            guessed = normalize_order_kind(cat_names.get(int(cid), ""))
            if guessed == "other":
                continue
            g.order_kind = guessed
            n += 1
        if n:
            await db.commit()
        data_dir.mkdir(parents=True, exist_ok=True)
        flag.write_text(f"seeded={n}\n", encoding="utf-8")
        logger.info("seed_order_kind_v155 seeded=%s", n)
        return n
    except Exception as exc:  # noqa: BLE001
        logger.warning("seed_order_kind_v155 failed: %s", exc)
        return 0


async def relayout_order_from_stored(db: AsyncSession) -> int:
    """Re-apply 20/35×cost/9 layout for ALL fingerprints via group.order_kind/cost.

    Ping only ranks inside a slot — missing ping no longer pushes a config
    out of its مستقیم/تانل/گیمینگ window.
    """
    from app.services.ping_category_order import arrange_by_category_slots

    try:
        workspace = await list_workspace(db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("relayout list failed: %s", exc)
        return 0
    current = list(workspace.items)
    if not current:
        return 0

    ping_by_key: dict[tuple[str, int, str], tuple[float, int | None]] = {}
    for it in current:
        try:
            key = _fp_key(
                normalize_host(it.host),
                int(it.port),
                normalize_match_host(it.match_host or ""),
            )
        except (ValueError, TypeError):
            continue
        avg = getattr(it, "avg_ping", None)
        try:
            avg_f = float(avg) if avg is not None else None
        except (TypeError, ValueError):
            avg_f = None
        if avg_f is None:
            continue
        dcount = getattr(it, "device_count", None)
        try:
            dcount_i = int(dcount) if dcount is not None else None
        except (TypeError, ValueError):
            dcount_i = None
        ping_by_key[key] = (avg_f, dcount_i)

    def _avg(it: ConfigOrderItemOut) -> float:
        try:
            v = float(getattr(it, "avg_ping", None) or -1)
        except (TypeError, ValueError):
            v = -1.0
        return v if v > 0 else 1e18

    arranged = arrange_by_category_slots(
        current,
        avg_of=_avg,
        label_of=lambda it: it.label or "",
        device_of=lambda it: getattr(it, "device_count", None) or 1,
        category_of=lambda it: getattr(it, "category_name", None) or "",
        cost_of=lambda it: getattr(it, "usage_cost", None),
    )
    ordered = list(arranged)
    payload = [
        ConfigOrderItemIn(
            host=it.host,
            port=int(it.port),
            match_host=it.match_host or "",
            label=it.label,
        )
        for it in ordered
    ]
    await save_order(db, payload)

    result = await db.execute(select(ConfigLinkOrder))
    rows = list(result.scalars().all())
    restored = 0
    for row in rows:
        try:
            key = _fp_key(
                normalize_host(row.host),
                int(row.port),
                normalize_match_host(row.match_host or ""),
            )
        except (ValueError, TypeError):
            continue
        meta = ping_by_key.get(key)
        if meta is None:
            continue
        row.avg_ping = float(meta[0])
        if meta[1] is not None:
            row.device_count = int(meta[1])
        restored += 1
    await db.commit()
    await refresh_cache(db)
    logger.info(
        "relayout_order_from_stored ordered=%s restored_pings=%s",
        len(payload),
        restored,
    )
    return len(payload)


async def relayout_order_slots_once(db: AsyncSession) -> int:
    """One-shot after order_kind: re-layout with explicit group kinds.

    Flag: ``data/relayout_slots_v161`` (re-run when slot rules change).
    """
    from pathlib import Path

    data_dir = Path(__file__).resolve().parents[3] / "data"
    flag = data_dir / "relayout_slots_v161"
    if flag.exists():
        return 0
    n = await relayout_order_from_stored(db)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        flag.write_text(f"n={n}\n", encoding="utf-8")
    except OSError:
        pass
    return n


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
    """Drop stale order rows after rebuild; keep surviving order; append new endpoints.

    Uses fuzzy fingerprint match so plain-SNI rows map onto live ``sni|ob|…``
    sample slots instead of being wiped (which desynced order vs subscription).
    """
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
        sample = _find_sample(samples, host, port, match)
        if sample is None:
            continue
        try:
            sh = normalize_host(str(sample.get("host") or host))
            sp = int(sample.get("port") or port)
            sm = normalize_match_host(str(sample.get("match_host") or match))
        except (ValueError, TypeError):
            continue
        key = _fp_key(sh, sp, sm)
        if key in seen:
            continue
        seen.add(key)
        db.add(
            ConfigLinkOrder(
                host=sh,
                port=sp,
                match_host=sm,
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

    await orchestrator.provision_users_now(db, [user], targets, wait=45.0)

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
        await orchestrator.provision_users_now(
            db, [user], trapped_rebecca, wait=20.0
        )

    for attempt in range(5):
        user = await _get_order_sample_user(db)
        if not user:
            break
        try:
            await asyncio.wait_for(
                orchestrator.hydrate_share_links(db, user),
                timeout=25.0,
            )
        except Exception:  # noqa: BLE001
            pass
        user = await _get_order_sample_user(db) or user
        # Astra: merge live outbound share-links (every outbound remark)
        try:
            from app.services import astra_node as astra_node_svc

            for panel, _cfg, _gid in targets:
                if getattr(panel, "type", None) != PanelType.astra:
                    continue
                email = user.username
                for acc in user.panel_accounts or []:
                    if int(acc.panel_id) == int(panel.id) and (
                        acc.remote_username or ""
                    ).strip():
                        email = acc.remote_username
                        break
                _c, astra_links, _u = await astra_node_svc.client_usage_and_links(
                    db, panel, email
                )
                shares = [
                    str(x).strip()
                    for x in (astra_links or [])
                    if x and is_share_link(str(x))
                ]
                if not shares:
                    continue
                for acc in user.panel_accounts or []:
                    if int(acc.panel_id) != int(panel.id):
                        continue
                    have = {
                        str(x).strip()
                        for x in (acc.links or [])
                        if x and is_share_link(str(x))
                    }
                    merged = list(have)
                    for lk in shares:
                        if lk not in have:
                            merged.append(lk)
                    acc.links = merged
                    if acc.status != PanelAccountStatus.active:
                        acc.status = PanelAccountStatus.active
                        acc.error_message = None
        except Exception:  # noqa: BLE001
            logger.warning("order-sample astra merge failed", exc_info=True)

        def _shares(acc: PanelAccount) -> list[str]:
            return [
                str(x).strip()
                for x in (acc.links or [])
                if x and is_share_link(str(x))
            ]

        accounts = [
            a
            for a in (user.panel_accounts or [])
            if getattr(a.status, "value", a.status)
            not in (PanelAccountStatus.deleted.value, "deleted")
        ]
        share_total = sum(len(_shares(a)) for a in accounts)
        still_need = any(not _shares(a) for a in accounts if a.status == PanelAccountStatus.active)
        if share_total > 0 and not still_need:
            break
        if attempt < 4:
            await asyncio.sleep(0.4 + 0.2 * attempt)

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
