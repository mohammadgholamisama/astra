"""Global subscription link order keyed by host:port[+match_host]."""

from __future__ import annotations

import logging
import threading
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.config_link_order import ConfigLinkOrder
from app.models.group_config import GroupConfigItem
from app.schemas.config_order import (
    ConfigOrderItemIn,
    ConfigOrderItemOut,
    ConfigOrderWorkspaceOut,
)
from app.services.config_locations import (
    extract_link_endpoint,
    extract_link_match_host,
    normalize_host,
    normalize_match_host,
)
from app.services.group_configs import _parse_link_meta

logger = logging.getLogger(__name__)

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
    with _lock:
        return _order_idx.get(_fp_key(host, port, match), 10**6)


def _normalize_label(raw: str | None) -> str | None:
    text = " ".join(str(raw or "").split()).strip()
    if not text:
        return None
    return text[:128]


async def _sample_fingerprints(db: AsyncSession) -> dict[tuple[str, int, str], dict[str, Any]]:
    """Unique endpoints from GroupConfigItem sample links across all groups."""
    result = await db.execute(
        select(GroupConfigItem)
        .options(
            selectinload(GroupConfigItem.panel),
            selectinload(GroupConfigItem.group),
        )
        .order_by(GroupConfigItem.sort_order, GroupConfigItem.id)
    )
    items = list(result.scalars().all())
    out: dict[tuple[str, int, str], dict[str, Any]] = {}

    for item in items:
        link = (item.sample_link or "").strip()
        if not link:
            continue
        ep = extract_link_endpoint(link)
        if not ep:
            continue
        host, port = ep
        match = extract_link_match_host(link) or ""
        key = _fp_key(host, port, match)
        proto, remark, _ = _parse_link_meta(link)
        panel_name = ""
        if item.panel is not None:
            panel_name = str(getattr(item.panel, "name", "") or "")
        group_name = ""
        if item.group is not None:
            group_name = str(getattr(item.group, "name", "") or "")

        if key not in out:
            out[key] = {
                "host": host,
                "port": port,
                "match_host": match,
                "label": (item.label or remark or "").strip() or None,
                "sample_link": link,
                "protocol": item.protocol or proto or None,
                "panel_names": [],
                "group_names": [],
            }
        entry = out[key]
        if panel_name and panel_name not in entry["panel_names"]:
            entry["panel_names"].append(panel_name)
        if group_name and group_name not in entry["group_names"]:
            entry["group_names"].append(group_name)
        if not entry.get("label") and (item.label or remark):
            entry["label"] = (item.label or remark or "").strip() or None
        if not entry.get("sample_link"):
            entry["sample_link"] = link
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
            ConfigOrderItemOut(
                host=host,
                port=port,
                match_host=match,
                label=row.label or (sample or {}).get("label"),
                sort_order=i,
                in_sample=sample is not None,
                sample_link=(sample or {}).get("sample_link"),
                protocol=(sample or {}).get("protocol"),
                panel_names=list((sample or {}).get("panel_names") or []),
                group_names=list((sample or {}).get("group_names") or []),
            )
        )
        seen.add(key)

    # 2) New sample endpoints not yet ordered — append
    for key, sample in samples.items():
        if key in seen:
            continue
        items.append(
            ConfigOrderItemOut(
                host=sample["host"],
                port=sample["port"],
                match_host=sample["match_host"],
                label=sample.get("label"),
                sort_order=len(items),
                in_sample=True,
                sample_link=sample.get("sample_link"),
                protocol=sample.get("protocol"),
                panel_names=list(sample.get("panel_names") or []),
                group_names=list(sample.get("group_names") or []),
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
    """Replace the full global order list."""
    await db.execute(delete(ConfigLinkOrder))
    await db.flush()

    seen: set[tuple[str, int, str]] = set()
    for i, raw in enumerate(items):
        try:
            host = normalize_host(raw.host)
            port = int(raw.port)
            match = normalize_match_host(raw.match_host or "")
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if port < 1 or port > 65535:
            raise ValueError("پورت نامعتبر است")
        key = _fp_key(host, port, match)
        if key in seen:
            continue
        seen.add(key)
        db.add(
            ConfigLinkOrder(
                host=host,
                port=port,
                match_host=match,
                sort_order=i,
                label=_normalize_label(raw.label),
            )
        )
    await db.commit()
    await refresh_cache(db)
    return await list_workspace(db)


async def create_or_refresh_samples(db: AsyncSession) -> dict:
    from app.services.group_configs import create_or_refresh_sample_all

    stats = await create_or_refresh_sample_all(db)
    # Refresh workspace view after samples
    return stats
