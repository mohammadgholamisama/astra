"""Global subscription hide: (host, port, match_host) → excluded from every sub."""

from __future__ import annotations

import logging
import threading
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.group import Group
from app.models.group_config import GroupConfigItem
from app.models.group_config_hide import GroupConfigHide
from app.services.config_locations import (
    extract_link_endpoint,
    extract_link_match_host,
    normalize_host,
    normalize_match_host,
    try_normalize_endpoint,
)

logger = logging.getLogger(__name__)

_lock = threading.RLock()
# (host, port, match_host) → hidden from ALL subscriptions
_hidden: set[tuple[str, int, str]] = set()
_loaded = False


def apply_cache(rows: list[GroupConfigHide]) -> None:
    next_set: set[tuple[str, int, str]] = set()
    for row in rows:
        try:
            host = normalize_host(row.host)
            port = int(row.port)
            match = normalize_match_host(getattr(row, "match_host", None) or "")
        except (ValueError, TypeError):
            continue
        if port < 1 or port > 65535:
            continue
        next_set.add((host, port, match))
    with _lock:
        global _hidden, _loaded
        _hidden = next_set
        _loaded = True


async def refresh_cache(db: AsyncSession) -> int:
    result = await db.execute(select(GroupConfigHide))
    rows = list(result.scalars().all())
    apply_cache(rows)
    return len(rows)


def is_hidden(link: str, group_id: int | None = None) -> bool:
    """True if this share link's host:port[+header] is globally hidden.

    ``group_id`` kept for call-site compatibility — hide is global for all subs.
    Exact match on optional header/SNI host.
    """
    _ = group_id
    ep = extract_link_endpoint(link)
    if not ep:
        return False
    host, port = ep
    match = extract_link_match_host(link) or ""
    norm = try_normalize_endpoint(host, port, match)
    if not norm:
        return False
    host, port, match = norm
    with _lock:
        return (host, port, match) in _hidden


def all_hidden_endpoints() -> set[tuple[str, int, str]]:
    with _lock:
        return set(_hidden)


def hidden_endpoints_for_group(group_id: int) -> set[tuple[str, int, str]]:
    """Compatibility: same as global set (hide is not group-scoped)."""
    _ = group_id
    return all_hidden_endpoints()


async def list_hides(db: AsyncSession, group_id: int | None = None) -> list[GroupConfigHide]:
    _ = group_id
    result = await db.execute(
        select(GroupConfigHide).order_by(
            GroupConfigHide.host, GroupConfigHide.port, GroupConfigHide.match_host
        )
    )
    return list(result.scalars().all())


async def set_hidden(
    db: AsyncSession,
    group_id: int | None,
    host: str,
    port: int,
    *,
    hidden: bool,
    match_host: str | None = "",
) -> GroupConfigHide | None:
    """Add or remove a GLOBAL hide rule (affects every subscription)."""
    host_n = normalize_host(host)
    port_n = int(port)
    match_n = normalize_match_host(match_host)
    if port_n < 1 or port_n > 65535:
        raise ValueError("پورت باید بین ۱ تا ۶۵۵۳۵ باشد")

    # Collapse any legacy per-group duplicates for this endpoint
    result = await db.execute(
        select(GroupConfigHide).where(
            GroupConfigHide.host == host_n,
            GroupConfigHide.port == port_n,
            GroupConfigHide.match_host == match_n,
        )
    )
    rows = list(result.scalars().all())

    if hidden:
        row = rows[0] if rows else None
        for extra in rows[1:]:
            await db.delete(extra)
        if row is None:
            # group_id is audit-only; some DBs still have NOT NULL — always resolve a safe id
            gid: int | None = None
            if group_id is not None:
                g = await db.get(Group, int(group_id))
                if g:
                    gid = int(g.id)
            if gid is None:
                any_g = (
                    await db.execute(select(Group).order_by(Group.id.asc()).limit(1))
                ).scalar_one_or_none()
                if any_g is not None:
                    gid = int(any_g.id)
            row = GroupConfigHide(
                group_id=gid,
                host=host_n,
                port=port_n,
                match_host=match_n,
            )
            db.add(row)
            await db.flush()
        elif group_id is not None and row.group_id is None:
            g = await db.get(Group, int(group_id))
            if g:
                row.group_id = int(g.id)
        await db.commit()
        await refresh_cache(db)
        return row

    for row in rows:
        await db.delete(row)
    await db.commit()
    await refresh_cache(db)
    return None


def _link_protocol(link: str) -> str:
    raw = (link or "").strip()
    if "://" not in raw:
        return ""
    return raw.split("://", 1)[0].strip().lower()


def _link_label(link: str) -> str:
    raw = (link or "").strip()
    if "#" in raw:
        frag = raw.rsplit("#", 1)[-1].strip()
        if frag:
            try:
                from urllib.parse import unquote

                return unquote(frag)[:120]
            except Exception:  # noqa: BLE001
                return frag[:120]
    return ""


def _bucket_from_link(
    link: str,
    *,
    label: str,
    protocol: str,
    panel_name: str | None,
    hidden: set[tuple[str, int, str]],
) -> tuple[tuple[str, int, str], dict[str, Any]] | None:
    ep = extract_link_endpoint(link)
    if not ep:
        return None
    host, port = ep
    match = extract_link_match_host(link) or ""
    key = (host, port, match)
    return key, {
        "host": host,
        "port": port,
        "match_host": match,
        "label": label,
        "protocol": protocol,
        "count": 1,
        "panel_names": [panel_name] if panel_name else [],
        "hidden": key in hidden,
    }


async def build_visibility(
    db: AsyncSession, group_id: int
) -> dict[str, Any]:
    """Aggregate sample configs for one group; hide flags are global."""
    group = await db.get(Group, group_id)
    if not group:
        raise ValueError("گروه پیدا نشد")

    result = await db.execute(
        select(GroupConfigItem)
        .where(GroupConfigItem.group_id == group_id)
        .options(selectinload(GroupConfigItem.panel))
        .order_by(GroupConfigItem.sort_order, GroupConfigItem.id)
    )
    items = list(result.scalars().all())
    hidden = all_hidden_endpoints()
    for h in await list_hides(db):
        try:
            hidden.add(
                (
                    normalize_host(h.host),
                    int(h.port),
                    normalize_match_host(getattr(h, "match_host", None) or ""),
                )
            )
        except ValueError:
            continue

    buckets: dict[tuple[str, int, str], dict[str, Any]] = {}
    for item in items:
        link = (item.sample_link or "").strip()
        if not link:
            continue
        label = (item.label or "").strip() or _link_label(link)
        proto = (item.protocol or "").strip().lower() or _link_protocol(link)
        panel_name = item.panel.name if item.panel else None
        parsed = _bucket_from_link(
            link, label=label, protocol=proto, panel_name=panel_name, hidden=hidden
        )
        if not parsed:
            continue
        key, row = parsed
        bucket = buckets.get(key)
        if bucket is None:
            buckets[key] = row
        else:
            bucket["count"] += 1
            if label and (not bucket["label"] or len(label) < len(bucket["label"])):
                bucket["label"] = label
            if proto and not bucket["protocol"]:
                bucket["protocol"] = proto
            if panel_name and panel_name not in bucket["panel_names"]:
                bucket["panel_names"].append(panel_name)

    # Orphan global hides (not in this group's sample) still listed when viewing group
    for host, port, match in sorted(hidden):
        if (host, port, match) not in buckets:
            buckets[(host, port, match)] = {
                "host": host,
                "port": port,
                "match_host": match,
                "label": "",
                "protocol": "",
                "count": 0,
                "panel_names": [],
                "hidden": True,
                "orphan": True,
            }

    rows = list(buckets.values())
    rows.sort(key=lambda r: (r["host"], r["port"], r.get("match_host") or ""))
    for r in rows:
        r.setdefault("orphan", False)
        r.setdefault("match_host", "")
        # Recompute hide from live set
        r["hidden"] = (r["host"], int(r["port"]), r.get("match_host") or "") in hidden

    from app.services.config_locations import attach_location_to_rows

    attach_location_to_rows(rows)

    # sample_ready = probe user still exists on remotes (not merely DB items)
    from app.services.group_configs import sample_status_for_group

    sample_alive, sample_username = await sample_status_for_group(db, group_id)
    sample_rows = [r for r in rows if not r.get("orphan")]

    return {
        "group_id": group.id,
        "group_name": group.name,
        "sample_ready": sample_alive,
        "sample_username": sample_username,
        "items": rows,
        "hidden_count": sum(1 for r in sample_rows if r["hidden"]),
        "total_count": len(sample_rows),
    }


async def build_visibility_all(db: AsyncSession) -> dict[str, Any]:
    """Merge configs from every active group (DB only — no remote I/O)."""
    result = await db.execute(
        select(Group).where(Group.is_disabled.is_(False)).order_by(Group.name, Group.id)
    )
    groups = list(result.scalars().all())
    hidden = all_hidden_endpoints()
    for h in await list_hides(db):
        try:
            hidden.add(
                (
                    normalize_host(h.host),
                    int(h.port),
                    normalize_match_host(getattr(h, "match_host", None) or ""),
                )
            )
        except ValueError:
            continue

    # Aggregate by endpoint globally, list which groups/panels carry it
    buckets: dict[tuple[str, int, str], dict[str, Any]] = {}
    sample_ready_groups = 0
    from app.services.group_configs import sample_status_for_group

    for group in groups:
        result = await db.execute(
            select(GroupConfigItem)
            .where(GroupConfigItem.group_id == group.id)
            .options(selectinload(GroupConfigItem.panel))
        )
        items = list(result.scalars().all())
        alive, _uname = await sample_status_for_group(db, group.id)
        if alive:
            sample_ready_groups += 1
        for item in items:
            link = (item.sample_link or "").strip()
            if not link:
                continue
            label = (item.label or "").strip() or _link_label(link)
            proto = (item.protocol or "").strip().lower() or _link_protocol(link)
            panel_name = item.panel.name if item.panel else None
            parsed = _bucket_from_link(
                link, label=label, protocol=proto, panel_name=panel_name, hidden=hidden
            )
            if not parsed:
                continue
            key, row = parsed
            bucket = buckets.get(key)
            if bucket is None:
                buckets[key] = {
                    **row,
                    "group_id": int(group.id),
                    "group_name": group.name,
                    "group_names": [group.name],
                }
            else:
                bucket["count"] += 1
                if group.name not in (bucket.get("group_names") or []):
                    bucket.setdefault("group_names", []).append(group.name)
                if panel_name and panel_name not in bucket["panel_names"]:
                    bucket["panel_names"].append(panel_name)
                if label and (not bucket["label"] or len(label) < len(bucket["label"])):
                    bucket["label"] = label

    for host, port, match in sorted(hidden):
        if (host, port, match) not in buckets:
            buckets[(host, port, match)] = {
                "host": host,
                "port": port,
                "match_host": match,
                "label": "",
                "protocol": "",
                "count": 0,
                "panel_names": [],
                "group_id": None,
                "group_name": None,
                "group_names": [],
                "hidden": True,
                "orphan": True,
            }

    rows = list(buckets.values())
    rows.sort(key=lambda r: (r["host"], r["port"], r.get("match_host") or ""))
    for r in rows:
        r.setdefault("orphan", False)
        r.setdefault("match_host", "")
        r["hidden"] = (r["host"], int(r["port"]), r.get("match_host") or "") in hidden
        # Prefer showing joined group names in group_name for UI
        names = r.pop("group_names", None) or []
        if names:
            r["group_name"] = " · ".join(names[:4]) + ("…" if len(names) > 4 else "")

    from app.services.config_locations import attach_location_to_rows

    attach_location_to_rows(rows)

    return {
        "items": rows,
        "hidden_count": sum(1 for r in rows if r["hidden"]),
        "total_count": len(rows),
        "groups_count": len(groups),
        "sample_ready_groups": sample_ready_groups,
    }
