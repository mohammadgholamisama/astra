"""Adaptive Hot / Warm / Cold usage tiers for Worker polling.

Hot  — remaining ≤ 1.5 GB OR used ≥ 90% → priority-probe every ~4s
Warm — used 50–90% OR /sub in last 15 min → 25s targeted waves
Cold — used < 50% AND idle > 15 min → full sweep every ~120s (4th wave)

All DB work is sequential on a single AsyncSession (never shared across
concurrent tasks) and uses column SELECTs — no nested selectinload, which
triggered asyncpg ``another operation is in progress`` under load.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger("astra.usage.tiers")

_GB = 1024**3
_HOT_REMAINING = int(1.5 * _GB)
_HOT_RATIO = 0.90
_WARM_RATIO = 0.50
_SUB_WARM_SEC = 15 * 60
# No hard row cap — newly-created / actively downloading users must stay Hot.
# Scan all active+limited with a positive data_limit (users table is ~10k, not 391k).
_ACCOUNT_CHUNK = 800

# user_id → last /sub monotonic
_SUB_TOUCH: dict[int, float] = {}
# panel_id → {hot: set[str], warm: set[str]}
_PANEL_TIERS: dict[int, dict[str, set[str]]] = {}
_TASK: asyncio.Task | None = None
_REFRESH_LOCK = asyncio.Lock()


def mark_sub_touch(user_id: int) -> None:
    uid = int(user_id)
    _SUB_TOUCH[uid] = time.monotonic()
    if len(_SUB_TOUCH) > 20000:
        cutoff = time.monotonic() - _SUB_WARM_SEC
        for k, ts in list(_SUB_TOUCH.items()):
            if ts < cutoff:
                _SUB_TOUCH.pop(k, None)


def classify_user(
    *,
    used: int,
    data_limit: int,
    user_id: int,
    now: float | None = None,
) -> str:
    """Return ``hot`` | ``warm`` | ``cold``."""
    now = time.monotonic() if now is None else now
    lim = int(data_limit or 0)
    used_i = max(0, int(used or 0))
    if lim > 0:
        remaining = lim - used_i
        ratio = used_i / float(lim)
        if remaining <= _HOT_REMAINING or ratio >= _HOT_RATIO:
            return "hot"
        if ratio >= _WARM_RATIO:
            return "warm"
    last_sub = float(_SUB_TOUCH.get(int(user_id), 0.0) or 0.0)
    if last_sub and (now - last_sub) <= _SUB_WARM_SEC:
        return "warm"
    return "cold"


def panel_tier_names(panel_id: int) -> dict[str, list[str]]:
    row = _PANEL_TIERS.get(int(panel_id)) or {}
    return {
        "hot": sorted(row.get("hot") or ()),
        "warm": sorted(row.get("warm") or ()),
    }


def all_panel_tiers() -> dict[int, dict[str, list[str]]]:
    return {pid: panel_tier_names(pid) for pid in _PANEL_TIERS}


async def ensure_tier_loop() -> None:
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_tier_loop(), name="usage-tier-loop")


async def _tier_loop() -> None:
    """Refresh Hot/Warm maps and keep Hot users on the priority-probe queue."""
    await asyncio.sleep(5.0)
    while True:
        try:
            # Skip overlapping cycles if a prior refresh is still running.
            if _REFRESH_LOCK.locked():
                await asyncio.sleep(4.0)
                continue
            async with _REFRESH_LOCK:
                await _refresh_tiers_and_enqueue_hot()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("tier loop failed")
        await asyncio.sleep(4.0)


async def _refresh_tiers_and_enqueue_hot() -> None:
    """Sequential column queries; one isolated AsyncSession per DB round-trip.

    Never shares a session across concurrent tasks, never holds a checkout
    across ``await asyncio.sleep``, and never uses nested ``selectinload``.
    """
    from sqlalchemy import and_, select

    from app.core.database import AsyncSessionLocal
    from app.models.panel import Panel
    from app.models.user import PanelAccount, PanelAccountStatus, User, UserStatus
    from app.services import usage_priority as uprio

    now = time.monotonic()
    new_map: dict[int, dict[str, set[str]]] = {}
    hot_n = warm_n = cold_n = 0
    interesting: dict[int, str] = {}

    # --- Pass 1: classify users (scalars only, no 6k cap) ---
    # Prefer recently-/sub'd and high-usage users first so Hot maps fill early.
    from sqlalchemy import case

    async with AsyncSessionLocal() as db:
        ratio = case(
            (User.data_limit > 0, User.used_traffic * 1.0 / User.data_limit),
            else_=0.0,
        )
        rows = (
            await db.execute(
                select(User.id, User.used_traffic, User.data_limit)
                .where(
                    User.status.in_(
                        (UserStatus.active, UserStatus.on_hold, UserStatus.limited)
                    ),
                    User.data_limit > 0,
                )
                .order_by(
                    User.updated_at.desc().nulls_last(),
                    ratio.desc(),
                    User.id.desc(),
                )
            )
        ).all()
    # Session fully closed before CPU work.

    # Boost users with a live /sub touch to the front of classification.
    sub_boost = {
        uid: ts
        for uid, ts in _SUB_TOUCH.items()
        if now - float(ts) <= _SUB_WARM_SEC
    }
    rows_sorted = sorted(
        rows,
        key=lambda r: (
            0 if int(r[0]) in sub_boost else 1,
            -float(sub_boost.get(int(r[0]), 0.0) or 0.0),
            -(
                (float(r[1] or 0) / float(r[2]))
                if int(r[2] or 0) > 0
                else 0.0
            ),
        ),
    )

    cold_ids: list[int] = []
    for uid, used, lim in rows_sorted:
        tier = classify_user(
            used=int(used or 0),
            data_limit=int(lim or 0),
            user_id=int(uid),
            now=now,
        )
        if tier == "hot":
            hot_n += 1
            interesting[int(uid)] = "hot"
        elif tier == "warm":
            warm_n += 1
            interesting[int(uid)] = "warm"
        else:
            cold_n += 1
            cold_ids.append(int(uid))

    # Rotate a large slice of cold users into warm each cycle (~60–90s)
    # so cold remotes still get exact probes and last_sync_at moves.
    if cold_ids:
        rot = int(now // 60.0)  # ~60s bucket
        slice_n = min(1500, max(200, len(cold_ids) // 6))
        start = (rot * slice_n) % len(cold_ids)
        for uid in cold_ids[start : start + slice_n]:
            interesting.setdefault(int(uid), "warm")

    if not interesting:
        _PANEL_TIERS.clear()
        logger.info(
            "tiers refreshed hot=%s warm=%s cold_skipped=%s panels=0",
            hot_n,
            warm_n,
            cold_n,
        )
        return

    # --- Pass 2: remotes for hot/warm (each chunk = own session) ---
    user_ids = list(interesting.keys())
    for off in range(0, len(user_ids), _ACCOUNT_CHUNK):
        chunk = user_ids[off : off + _ACCOUNT_CHUNK]
        async with AsyncSessionLocal() as db:
            acc_rows = (
                await db.execute(
                    select(
                        PanelAccount.user_id,
                        PanelAccount.panel_id,
                        PanelAccount.remote_username,
                        Panel.worker_id,
                    )
                    .join(Panel, Panel.id == PanelAccount.panel_id)
                    .where(
                        PanelAccount.user_id.in_(chunk),
                        PanelAccount.status.in_(
                            (PanelAccountStatus.active, PanelAccountStatus.error)
                        ),
                        Panel.worker_id.is_not(None),
                        and_(
                            PanelAccount.remote_username.is_not(None),
                            PanelAccount.remote_username != "",
                        ),
                    )
                )
            ).all()
        # Session closed — safe to enqueue / mutate in-memory maps.
        for uid, pid, remote, wid in acc_rows:
            name = (remote or "").strip()
            if not name or not wid:
                continue
            tier = interesting.get(int(uid))
            if tier not in ("hot", "warm"):
                continue
            bucket = new_map.setdefault(int(pid), {"hot": set(), "warm": set()})
            bucket[tier].add(name)
            if tier == "hot":
                uprio.enqueue_probe(
                    worker_id=int(wid),
                    panel_id=int(pid),
                    remote_username=name,
                    ttl=12.0,
                )
        await asyncio.sleep(0)

    _PANEL_TIERS.clear()
    _PANEL_TIERS.update(new_map)
    logger.info(
        "tiers refreshed hot=%s warm=%s cold_skipped=%s panels=%s",
        hot_n,
        warm_n,
        cold_n,
        len(new_map),
    )


def tier_stats() -> dict[str, Any]:
    hot = sum(len(v.get("hot") or ()) for v in _PANEL_TIERS.values())
    warm = sum(len(v.get("warm") or ()) for v in _PANEL_TIERS.values())
    return {
        "panels": len(_PANEL_TIERS),
        "hot_remotes": hot,
        "warm_remotes": warm,
        "sub_touches": len(_SUB_TOUCH),
    }
