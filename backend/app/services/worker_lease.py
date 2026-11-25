"""Panel→Worker routing leases with preferred failback.

``panels.worker_id`` = active lease holder (routing + usage ownership).
``panels.preferred_worker_id`` = sticky affinity (NULL = auto-balance).

Heartbeat freshness is tracked in-process + Redis so any uvicorn worker that
receives a probe/usage-report can refresh the lease clock.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("astra.worker.lease")

# Missed heartbeats → revoke. Worker HB ~3–4s + commands long-poll ≤15s;
# 18s absorbs a heavy cold-sweep gap without false revoke churn, still
# fails over in <18s if the worker is genuinely gone. Failback needs 2
# consecutive healthy observations.
_HB_STALE_SEC = 18.0
_LEASE_TTL_SEC = 30.0
_FAILBACK_STREAK = 2
# Large/slow remotes — spread evenly across online workers when a 2nd node joins.
_HEAVY_PANEL_IDS = frozenset({3, 4, 5, 17, 25, 39, 60, 62, 63})

# worker_id → last heartbeat monotonic
_HB_TS: dict[int, float] = {}
# worker_id → consecutive healthy observations
_OK_STREAK: dict[int, int] = {}
# panel_id → {worker_id, expires_mono, preferred}
_LEASES: dict[int, dict[str, Any]] = {}
_LOCK = asyncio.Lock()
_TASK: asyncio.Task | None = None
_PUSH_NEEDED: set[int] = set()  # worker ids needing assignment push


def note_heartbeat(worker_id: int) -> None:
    """Record a fresh heartbeat (wall-clock) visible to every master process.

    ``astra`` (API) receives heartbeats; ``astra-bg`` owns lease rebalance.
    They do not share memory, so Redis must carry a **wall-clock** stamp —
    ``time.monotonic()`` is process-local and caused healthy=0 flapping.
    """
    wid = int(worker_id)
    now_wall = time.time()
    _HB_TS[wid] = now_wall
    _OK_STREAK[wid] = int(_OK_STREAK.get(wid, 0) or 0) + 1
    try:
        from app.core.redis_cache import get_redis

        r = get_redis()
        if r is not None:
            # TTL > stale window so bg can still read during a brief gap.
            r.set(
                f"astra:whb:{wid}",
                f"{now_wall:.3f}".encode(),
                ex=max(30, int(_HB_STALE_SEC) + 15),
            )
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.services.worker_client import mark_worker_health

        mark_worker_health(wid, True, "")
    except Exception:  # noqa: BLE001
        pass


def _hb_wall_ts(worker_id: int) -> float | None:
    """ freshest wall-clock heartbeat for worker (memory + Redis)."""
    wid = int(worker_id)
    best: float | None = None
    local = _HB_TS.get(wid)
    if local is not None:
        best = float(local)
    try:
        from app.core.redis_cache import get_redis

        r = get_redis()
        if r is not None:
            raw = r.get(f"astra:whb:{wid}")
            if raw:
                remote = float(raw)
                # Ignore leftover monotonic junk from older builds (>> now).
                if remote > 1_000_000_000:  # roughly year 2001+ unix
                    if best is None or remote > best:
                        best = remote
                        _HB_TS[wid] = remote
    except Exception:  # noqa: BLE001
        pass
    return best


def last_heartbeat_age(worker_id: int) -> float | None:
    ts = _hb_wall_ts(worker_id)
    if ts is None:
        return None
    return max(0.0, time.time() - float(ts))


def is_worker_fresh(worker_id: int) -> bool:
    age = last_heartbeat_age(worker_id)
    if age is None:
        return False
    return age <= _HB_STALE_SEC


def healthy_worker_ids() -> list[int]:
    now = time.time()
    out: list[int] = []
    # Prefer Redis wall-clock so API heartbeats are visible to astra-bg.
    try:
        from app.core.redis_cache import get_redis

        r = get_redis()
        if r is not None:
            for key in r.scan_iter(match="astra:whb:*", count=64):
                try:
                    k = key.decode() if isinstance(key, (bytes, bytearray)) else str(key)
                    wid = int(k.rsplit(":", 1)[-1])
                    raw = r.get(key)
                    if not raw:
                        continue
                    ts = float(raw)
                    if ts > 1_000_000_000 and (now - ts) <= _HB_STALE_SEC:
                        _HB_TS[wid] = ts
                        out.append(wid)
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001
        pass
    for wid, ts in list(_HB_TS.items()):
        if now - float(ts) <= _HB_STALE_SEC:
            out.append(int(wid))
    if out:
        return list(dict.fromkeys(out))
    # Fall back to worker_client cache (probe-based; monotonic — last resort).
    try:
        from app.services import worker_client as wc

        mono_now = time.monotonic()
        for wid, (ok, ts, _err) in list(wc._HEALTH.items()):  # noqa: SLF001
            if ok and (mono_now - float(ts) <= max(_HB_STALE_SEC, 20.0)):
                out.append(int(wid))
    except Exception:  # noqa: BLE001
        pass
    return list(dict.fromkeys(out))


def lease_worker_for_panel(panel_id: int) -> int | None:
    row = _LEASES.get(int(panel_id))
    if not row:
        return None
    if time.monotonic() > float(row.get("expires_mono") or 0):
        return None
    wid = row.get("worker_id")
    return int(wid) if wid else None


def panels_for_worker(worker_id: int) -> list[int]:
    wid = int(worker_id)
    now = time.monotonic()
    return [
        pid
        for pid, row in _LEASES.items()
        if int(row.get("worker_id") or 0) == wid
        and float(row.get("expires_mono") or 0) >= now
    ]


def _set_lease(
    panel_id: int,
    worker_id: int,
    *,
    preferred: int | None,
    ttl: float = _LEASE_TTL_SEC,
) -> None:
    _LEASES[int(panel_id)] = {
        "worker_id": int(worker_id),
        "preferred": int(preferred) if preferred else None,
        "expires_mono": time.monotonic() + float(ttl),
    }
    try:
        from app.core.redis_cache import get_redis

        r = get_redis()
        if r is not None:
            r.set(
                f"astra:lease:{int(panel_id)}",
                str(int(worker_id)).encode(),
                ex=max(5, int(ttl)),
            )
    except Exception:  # noqa: BLE001
        pass


def refresh_leases_for_worker(worker_id: int) -> None:
    """Extend TTL for all panels currently leased to this worker."""
    wid = int(worker_id)
    now = time.monotonic()
    for pid, row in list(_LEASES.items()):
        if int(row.get("worker_id") or 0) == wid:
            row["expires_mono"] = now + _LEASE_TTL_SEC
            _LEASES[pid] = row


async def load_leases_from_db() -> int:
    """Seed in-memory leases from panels.worker_id."""
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.panel import Panel, PanelType

    n = 0
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(
                    Panel.id,
                    Panel.worker_id,
                    Panel.preferred_worker_id,
                ).where(
                    Panel.is_active.is_(True),
                    Panel.type != PanelType.astra,
                    Panel.worker_id.is_not(None),
                )
            )
        ).all()
        for pid, wid, pref in rows:
            if not wid:
                continue
            _set_lease(int(pid), int(wid), preferred=int(pref) if pref else None)
            n += 1
    return n


def _pick_worker(
    *,
    preferred: int | None,
    healthy: list[int],
    load: dict[int, int],
    heavy_load: dict[int, int] | None = None,
    panel_id: int | None = None,
) -> int | None:
    if not healthy:
        return None
    if preferred and int(preferred) in healthy:
        return int(preferred)
    heavy = heavy_load or {}
    is_heavy = int(panel_id or 0) in _HEAVY_PANEL_IDS
    # Heavy panels: prefer worker with fewer heavy leases, then least total load.
    if is_heavy:
        return min(
            healthy,
            key=lambda w: (heavy.get(int(w), 0), load.get(int(w), 0), int(w)),
        )
    return min(healthy, key=lambda w: (load.get(int(w), 0), int(w)))


def _heavy_load_from(
    panels: list[tuple[Any, ...]], healthy: list[int]
) -> dict[int, int]:
    out: dict[int, int] = {int(w): 0 for w in healthy}
    for pid, cur_wid, _pref in panels:
        if int(pid) not in _HEAVY_PANEL_IDS:
            continue
        wid = int(cur_wid) if cur_wid else None
        if wid and wid in out:
            out[wid] += 1
    return out


async def rebalance_once() -> dict[str, int]:
    """Revoke stale leases and redistribute; failback to preferred when ready."""
    from sqlalchemy import select, update

    from app.core.database import AsyncSessionLocal
    from app.models.panel import Panel, PanelType
    from app.models.worker import WorkerNode

    stats = {
        "revoked": 0,
        "moved": 0,
        "failback": 0,
        "healthy": 0,
        "heavy_moved": 0,
    }
    async with _LOCK:
        healthy = healthy_worker_ids()
        stats["healthy"] = len(healthy)
        load: dict[int, int] = {w: 0 for w in healthy}
        for row in _LEASES.values():
            wid = int(row.get("worker_id") or 0)
            if wid in load:
                load[wid] += 1

        async with AsyncSessionLocal() as db:
            workers = (
                await db.execute(
                    select(WorkerNode.id, WorkerNode.is_active).where(
                        WorkerNode.is_active.is_(True)
                    )
                )
            ).all()
            active_ids = {int(i) for i, _a in workers}
            # Merge probe-healthy workers that haven't heartbeated yet (boot).
            for wid in active_ids:
                if wid not in healthy and is_worker_fresh(wid):
                    healthy.append(wid)
                    load.setdefault(wid, 0)
            healthy = [w for w in healthy if w in active_ids]
            stats["healthy"] = len(healthy)

            panels = (
                await db.execute(
                    select(
                        Panel.id,
                        Panel.worker_id,
                        Panel.preferred_worker_id,
                    ).where(
                        Panel.is_active.is_(True),
                        Panel.type != PanelType.astra,
                    )
                )
            ).all()
            heavy_load = _heavy_load_from(list(panels), healthy)

            moves: list[tuple[int, int | None, int | None]] = []  # pid, old, new
            for pid, cur_wid, pref in panels:
                pid = int(pid)
                cur = int(cur_wid) if cur_wid else None
                preferred = int(pref) if pref else None
                cur_fresh = bool(cur and cur in healthy)

                if cur and not cur_fresh:
                    # Failover — revoke and reassign.
                    stats["revoked"] += 1
                    new_w = _pick_worker(
                        preferred=preferred,
                        healthy=healthy,
                        load=load,
                        heavy_load=heavy_load,
                        panel_id=pid,
                    )
                    if new_w != cur:
                        moves.append((pid, cur, new_w))
                        if cur in load:
                            load[cur] = max(0, load[cur] - 1)
                        if new_w:
                            load[new_w] = load.get(new_w, 0) + 1
                        if pid in _HEAVY_PANEL_IDS:
                            if cur in heavy_load:
                                heavy_load[cur] = max(0, heavy_load[cur] - 1)
                            if new_w:
                                heavy_load[new_w] = heavy_load.get(new_w, 0) + 1
                    continue

                if (
                    preferred
                    and preferred in healthy
                    and int(_OK_STREAK.get(preferred, 0) or 0) >= _FAILBACK_STREAK
                    and cur != preferred
                ):
                    # Failback to preferred after 2 consecutive healthy HBs.
                    stats["failback"] += 1
                    moves.append((pid, cur, preferred))
                    if cur and cur in load:
                        load[cur] = max(0, load[cur] - 1)
                    load[preferred] = load.get(preferred, 0) + 1
                    if pid in _HEAVY_PANEL_IDS:
                        if cur and cur in heavy_load:
                            heavy_load[cur] = max(0, heavy_load[cur] - 1)
                        heavy_load[preferred] = heavy_load.get(preferred, 0) + 1
                    continue

                if cur is None and healthy:
                    # Unassigned active panel → claim.
                    new_w = _pick_worker(
                        preferred=preferred,
                        healthy=healthy,
                        load=load,
                        heavy_load=heavy_load,
                        panel_id=pid,
                    )
                    if new_w:
                        moves.append((pid, None, new_w))
                        load[new_w] = load.get(new_w, 0) + 1
                        if pid in _HEAVY_PANEL_IDS:
                            heavy_load[new_w] = heavy_load.get(new_w, 0) + 1
                    continue

                # Refresh lease clock for healthy holders.
                if cur and cur_fresh:
                    _set_lease(pid, cur, preferred=preferred)

            # Multi-worker: spread auto-affinity heavy panels evenly as soon as
            # a second Worker is online (skip sticky preferred_worker_id).
            if len(healthy) >= 2:
                movable = [
                    (int(pid), int(cur_wid) if cur_wid else None, int(pref) if pref else None)
                    for pid, cur_wid, pref in panels
                    if int(pid) in _HEAVY_PANEL_IDS
                    and cur_wid
                    and int(cur_wid) in healthy
                    and pref is None  # auto-balance only
                ]
                if movable:
                    counts = {int(w): 0 for w in healthy}
                    by_worker: dict[int, list[int]] = {int(w): [] for w in healthy}
                    for pid, cur, _pref in movable:
                        # Prefer post-move owner if already queued.
                        owner = cur
                        for m_pid, _o, m_new in moves:
                            if m_pid == pid and m_new:
                                owner = int(m_new)
                                break
                        if owner and owner in counts:
                            counts[owner] += 1
                            by_worker[owner].append(pid)
                    target = max(1, (sum(counts.values()) + len(healthy) - 1) // len(healthy))
                    for donor in sorted(healthy, key=lambda w: (-counts.get(w, 0), w)):
                        while counts.get(donor, 0) > target:
                            recv = min(
                                healthy,
                                key=lambda w: (counts.get(w, 0), load.get(w, 0), w),
                            )
                            if counts.get(recv, 0) >= target:
                                break
                            if not by_worker.get(donor):
                                break
                            pid = by_worker[donor].pop()
                            # Skip if already moving this panel.
                            if any(m[0] == pid for m in moves):
                                continue
                            moves.append((pid, donor, recv))
                            counts[donor] -= 1
                            counts[recv] = counts.get(recv, 0) + 1
                            by_worker.setdefault(recv, []).append(pid)
                            if donor in load:
                                load[donor] = max(0, load[donor] - 1)
                            load[recv] = load.get(recv, 0) + 1
                            stats["heavy_moved"] += 1

            touched_workers: set[int] = set()
            for pid, old_w, new_w in moves:
                await db.execute(
                    update(Panel)
                    .where(Panel.id == pid)
                    .values(worker_id=new_w)
                )
                if new_w:
                    _set_lease(pid, new_w, preferred=None)
                    # Preserve preferred from row
                    pref_row = next(
                        (p for p in panels if int(p[0]) == pid), None
                    )
                    pref_v = (
                        int(pref_row[2])
                        if pref_row and pref_row[2] is not None
                        else None
                    )
                    _set_lease(pid, new_w, preferred=pref_v)
                    touched_workers.add(int(new_w))
                else:
                    _LEASES.pop(pid, None)
                if old_w:
                    touched_workers.add(int(old_w))
                stats["moved"] += 1

            if moves:
                await db.commit()
                _PUSH_NEEDED.update(touched_workers)
                logger.info(
                    "lease rebalance revoked=%s moved=%s failback=%s "
                    "heavy_moved=%s healthy=%s",
                    stats["revoked"],
                    stats["moved"],
                    stats["failback"],
                    stats["heavy_moved"],
                    stats["healthy"],
                )
            else:
                await db.rollback()
    return stats


async def drain_push_needed() -> list[int]:
    async with _LOCK:
        out = list(_PUSH_NEEDED)
        _PUSH_NEEDED.clear()
        return out


async def ensure_lease_loop() -> None:
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_lease_loop(), name="worker-lease-loop")


async def _lease_loop() -> None:
    await asyncio.sleep(2.0)
    try:
        n = await load_leases_from_db()
        logger.info("leases loaded n=%s", n)
    except Exception:  # noqa: BLE001
        logger.exception("lease load failed")
    while True:
        try:
            await rebalance_once()
            # Push assignment snapshots to workers whose leases changed.
            from app.core.database import AsyncSessionLocal
            from app.services import worker_ops as ops

            wids = await drain_push_needed()
            if wids:
                async with AsyncSessionLocal() as db:
                    for wid in wids:
                        try:
                            await ops.sync_assignments_to_worker(db, int(wid))
                        except Exception:  # noqa: BLE001
                            logger.debug(
                                "lease push failed worker=%s", wid, exc_info=True
                            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("lease loop error")
        await asyncio.sleep(3.0)


def lease_stats() -> dict[str, Any]:
    now = time.monotonic()
    return {
        "leases": len(_LEASES),
        "healthy_workers": len(healthy_worker_ids()),
        "hb_tracked": len(_HB_TS),
        "stale_sec": _HB_STALE_SEC,
        "active_leases": sum(
            1 for r in _LEASES.values() if float(r.get("expires_mono") or 0) >= now
        ),
    }
