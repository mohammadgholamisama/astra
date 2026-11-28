"""Priority on-demand usage probe queue for Worker fast-poll.

Master enqueues ``(worker_id, panel_id, remote_username)`` when:
  - a user hits ``/sub`` and meters are older than ~15s, or
  - a user is created / provisioned, or
  - an active user is near quota (≥ 85% of data_limit), or
  - the Hot-tier refresh loop promotes remotes.

Workers poll ``GET /api/internal/worker/priority-probe`` every ~3s.

Redis ZSET score = ``last_probed_at`` (unix seconds). Take always returns a
**round-robin fair sample across ALL panels** (≤15/panel, ≤400 total) so a
single hot panel cannot monopolize the oldest-score head of the ZSET.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger("astra.usage.priority")

# Local fallback: (worker_id, panel_id, name_lower) → last_probed monotonic (0 = never)
_QUEUE: dict[tuple[int, int, str], float] = {}
# Local expiry for fallback entries (wall clock mono)
_EXPIRE: dict[tuple[int, int, str], float] = {}
_LOCK = asyncio.Lock()
_DEFAULT_TTL = 60.0
_NEAR_LIMIT_RATIO = 0.85
_SEP = "\x1f"
_MAX_TAKE = 400  # total remotes per priority-probe poll
_PER_PANEL_CAP = 20  # every active panel gets a slot every ~2–3s tick
_QUEUE_CAP = 8000
_HOT_TTL = 900.0  # create + /sub → Hot for 15 minutes


def _redis_key(worker_id: int) -> str:
    return f"astra:prio:{int(worker_id)}"


def _member(panel_id: int, remote_username: str) -> str:
    return f"{int(panel_id)}{_SEP}{(remote_username or '').strip()}"


def enqueue_probe(
    *,
    worker_id: int,
    panel_id: int,
    remote_username: str,
    ttl: float = _DEFAULT_TTL,
    boost: bool = False,
) -> bool:
    """Deduped enqueue. ``boost=True`` (/sub / create) forces front (score=0)."""
    name = (remote_username or "").strip()
    if not name or not worker_id or not panel_id:
        return False
    wid = int(worker_id)
    pid = int(panel_id)
    ttl_f = max(5.0, float(ttl))
    key = (wid, pid, name.lower())
    now_mono = time.monotonic()
    inserted = key not in _QUEUE
    if inserted or boost:
        _QUEUE[key] = 0.0  # never / force front
    _EXPIRE[key] = now_mono + ttl_f
    if len(_QUEUE) > 12000:
        _prune(now_mono)

    try:
        from app.core.redis_cache import get_redis

        client = get_redis()
        if client is not None:
            rkey = _redis_key(wid)
            member = _member(pid, name)
            if boost or inserted:
                # score 0 = NULLS FIRST (never probed / /sub boost)
                client.zadd(rkey, {member: 0.0})
            else:
                # Keep existing last_probed_at; only insert if absent.
                client.zadd(rkey, {member: 0.0}, nx=True)
            client.expire(rkey, int(ttl_f) + 600)
            try:
                n = int(client.zcard(rkey) or 0)
                if n > _QUEUE_CAP:
                    # Drop MOST-RECENTLY probed (highest scores) — never the
                    # never-probed / boosts at the low end (that starved panels).
                    client.zremrangebyrank(rkey, int(_QUEUE_CAP), -1)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        logger.debug("prio redis enqueue failed", exc_info=True)
    return inserted


def _prune(now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    dead = [k for k, exp in _EXPIRE.items() if exp < now]
    for k in dead:
        _QUEUE.pop(k, None)
        _EXPIRE.pop(k, None)


def _parse_member(raw: object, wid: int) -> dict[str, Any] | None:
    if isinstance(raw, (bytes, bytearray)):
        text = raw.decode("utf-8", "ignore")
    else:
        text = str(raw)
    if _SEP not in text:
        return None
    pid_s, name = text.split(_SEP, 1)
    name = (name or "").strip()
    if not name:
        return None
    try:
        pid = int(pid_s)
    except (TypeError, ValueError):
        return None
    return {
        "worker_id": wid,
        "panel_id": pid,
        "remote_username": name,
    }


def _fair_rr_select(
    items: list[dict[str, Any]], *, limit: int, per_panel: int
) -> list[dict[str, Any]]:
    """Round-robin across panels so every panel rotates every tick.

    Linear scan + per-panel cap still starved the fleet when two panels owned
    the entire low-score head of the ZSET (panels=2 users=70 forever).
    """
    lim = max(1, min(_MAX_TAKE, int(limit)))
    cap = max(1, min(_PER_PANEL_CAP, int(per_panel)))
    by_panel: dict[int, list[dict[str, Any]]] = {}
    order: list[int] = []
    for it in items:
        try:
            pid = int(it.get("panel_id") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        if pid not in by_panel:
            by_panel[pid] = []
            order.append(pid)
        if len(by_panel[pid]) < cap:
            by_panel[pid].append(it)
    out: list[dict[str, Any]] = []
    idx = {pid: 0 for pid in order}
    while len(out) < lim:
        progressed = False
        for pid in order:
            i = int(idx[pid])
            bucket = by_panel[pid]
            if i >= len(bucket):
                continue
            out.append(bucket[i])
            idx[pid] = i + 1
            progressed = True
            if len(out) >= lim:
                break
        if not progressed:
            break
    return out


def _take_boosts_from_redis(
    worker_id: int, limit: int = 400
) -> list[dict[str, Any]] | None:
    """Only score=0 boosts (/sub / create) — never scan the full 8k ZSET.

    Full ``ZRANGE 0 -1`` every 3s made priority-probe Master RTT 1–2s and
    pushed Worker ticks above 3s. Fleet coverage comes from SQL fair window.
    """
    try:
        from app.core.redis_cache import get_redis

        client = get_redis()
    except Exception:  # noqa: BLE001
        return None
    if client is None:
        return None
    wid = int(worker_id)
    lim = max(1, min(_MAX_TAKE, int(limit)))
    rkey = _redis_key(wid)
    try:
        # score 0 = never probed / boost. Cap read so Master stays <50ms.
        members = client.zrangebyscore(rkey, 0, 0, start=0, num=min(800, lim * 3))
        if not members:
            return []
        items: list[dict[str, Any]] = []
        for raw in members:
            item = _parse_member(raw, wid)
            if item:
                items.append(item)
        return _fair_rr_select(items, limit=lim, per_panel=_PER_PANEL_CAP)
    except Exception:  # noqa: BLE001
        logger.debug("prio redis boost take failed", exc_info=True)
        return None


async def _take_from_db_fair(
    worker_id: int, *, limit: int, per_panel: int
) -> list[dict[str, Any]]:
    """SQL window: ≤``per_panel`` remotes per panel for this worker.

    ``ROW_NUMBER() OVER (PARTITION BY panel_id ORDER BY last_sync_at ASC
    NULLS FIRST)`` — fills panels missing from Redis so brand-new / quiet
    panels still rotate every 3s tick.
    """
    from sqlalchemy import text

    from app.core.database import AsyncSessionLocal

    wid = int(worker_id)
    lim = max(1, min(_MAX_TAKE, int(limit)))
    cap = max(1, min(_PER_PANEL_CAP, int(per_panel)))
    # LATERAL LIMIT per panel — index-friendly nested loop. A window over all
    # panel_accounts scanned 391k rows (~1.8s) and blew the <3s Worker tick.
    sql = text(
        """
        SELECT p.id AS panel_id, pa.remote_username
        FROM panels p
        CROSS JOIN LATERAL (
            SELECT a.remote_username
            FROM panel_accounts a
            JOIN users u ON u.id = a.user_id
            WHERE a.panel_id = p.id
              AND a.status IN ('active', 'error')
              AND u.status IN ('active', 'on_hold', 'limited')
              AND a.remote_username IS NOT NULL
              AND btrim(a.remote_username) <> ''
              AND left(a.remote_username, 1) <> '{'
            ORDER BY a.last_sync_at ASC NULLS FIRST, a.id ASC
            LIMIT :cap
        ) pa
        WHERE p.worker_id = :wid
          AND p.is_active IS TRUE
        ORDER BY p.id ASC
        LIMIT :lim
        """
    )
    out: list[dict[str, Any]] = []
    try:
        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(sql, {"wid": wid, "cap": cap, "lim": lim})
            ).all()
            await db.rollback()
        for pid, name in rows:
            n = str(name or "").strip()
            if not n:
                continue
            out.append(
                {
                    "worker_id": wid,
                    "panel_id": int(pid),
                    "remote_username": n,
                }
            )
    except Exception:  # noqa: BLE001
        logger.debug("prio db fair take failed", exc_info=True)
    return out


def _merge_fair(
    primary: list[dict[str, Any]],
    secondary: list[dict[str, Any]],
    *,
    limit: int,
    per_panel: int,
) -> list[dict[str, Any]]:
    """Prefer primary (Redis boosts / rotated), top up missing panels from secondary."""
    lim = max(1, min(_MAX_TAKE, int(limit)))
    cap = max(1, min(_PER_PANEL_CAP, int(per_panel)))
    seen: set[tuple[int, str]] = set()
    counts: dict[int, int] = {}
    out: list[dict[str, Any]] = []

    def _add(it: dict[str, Any]) -> bool:
        if len(out) >= lim:
            return False
        try:
            pid = int(it.get("panel_id") or 0)
        except (TypeError, ValueError):
            return True
        name = str(it.get("remote_username") or "").strip()
        if pid <= 0 or not name:
            return True
        key = (pid, name.lower())
        if key in seen:
            return True
        if int(counts.get(pid, 0) or 0) >= cap:
            return True
        seen.add(key)
        counts[pid] = int(counts.get(pid, 0) or 0) + 1
        out.append(
            {
                "worker_id": int(it.get("worker_id") or 0),
                "panel_id": pid,
                "remote_username": name,
            }
        )
        return True

    for it in primary:
        if not _add(it):
            return out
    # Top up panels that Redis under-served.
    for it in secondary:
        if not _add(it):
            break
    return out


async def take_for_worker(
    worker_id: int, *, limit: int = 400
) -> list[dict[str, Any]]:
    """Return ≤400 probes: ≤15/panel across ALL active panels, oldest first.

    Primary coverage = SQL ``ROW_NUMBER() … ≤15`` per panel. Redis only
    contributes score=0 boosts (/sub + create) so Master responds in ms.
    """
    wid = int(worker_id)
    lim = max(1, min(_MAX_TAKE, int(limit)))

    boosts = await asyncio.to_thread(_take_boosts_from_redis, wid, lim)
    db_items = await _take_from_db_fair(
        wid, limit=lim, per_panel=_PER_PANEL_CAP
    )

    if boosts is None:
        boosts = []
        now = time.monotonic()
        async with _LOCK:
            _prune(now)
            keys = [
                k
                for k in _QUEUE
                if k[0] == wid
                and _EXPIRE.get(k, 0) >= now
                and float(_QUEUE.get(k, 0.0) or 0.0) <= 0.0
            ]
            boosts = [
                {
                    "worker_id": k[0],
                    "panel_id": k[1],
                    "remote_username": k[2],
                }
                for k in keys
            ]
            boosts = _fair_rr_select(
                boosts, limit=lim, per_panel=_PER_PANEL_CAP
            )

    out = _merge_fair(
        boosts, db_items, limit=lim, per_panel=_PER_PANEL_CAP
    )

    # Mirror dispatched remotes into Redis so next tick rotates last_probed.
    if out:
        try:
            from app.core.redis_cache import get_redis

            client = get_redis()
            if client is not None:
                rkey = _redis_key(wid)
                now = time.time()
                pipe = client.pipeline(transaction=False)
                for it in out:
                    pipe.zadd(
                        rkey,
                        {
                            _member(
                                int(it["panel_id"]),
                                str(it["remote_username"]),
                            ): now
                        },
                    )
                pipe.execute()
        except Exception:  # noqa: BLE001
            pass
        now_mono = time.monotonic()
        async with _LOCK:
            for it in out:
                k = (
                    wid,
                    int(it["panel_id"]),
                    str(it["remote_username"]).lower(),
                )
                _QUEUE[k] = now_mono
    return out


async def enqueue_user_remotes(user_id: int, *, ttl: float = _DEFAULT_TTL) -> int:
    """Load active remotes for a user and enqueue priority probes (own session)."""
    return await enqueue_users_remotes([int(user_id)], ttl=ttl)


async def enqueue_users_remotes(
    user_ids: list[int], *, ttl: float = _DEFAULT_TTL
) -> int:
    """Bulk Hot-promote: one session for many dirty users (usage-report path).

    Only exact ``panel_accounts`` rows — never invents panels. Avoids the
    N× session open that made cold applies take multi-seconds.
    """
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.panel import Panel
    from app.models.user import PanelAccount, PanelAccountStatus, User, UserStatus

    uids = list({int(x) for x in user_ids if int(x) > 0})
    if not uids:
        return 0
    n = 0
    try:
        async with AsyncSessionLocal() as db:
            alive = (
                await db.execute(
                    select(User.id).where(
                        User.id.in_(uids),
                        User.status.in_(
                            (
                                UserStatus.active,
                                UserStatus.on_hold,
                                UserStatus.limited,
                            )
                        ),
                    )
                )
            ).scalars().all()
            alive_ids = [int(x) for x in alive]
            if not alive_ids:
                return 0
            rows = (
                await db.execute(
                    select(
                        PanelAccount.user_id,
                        PanelAccount.panel_id,
                        PanelAccount.remote_username,
                        PanelAccount.remote_id,
                        Panel.worker_id,
                    )
                    .join(Panel, Panel.id == PanelAccount.panel_id)
                    .where(
                        PanelAccount.user_id.in_(alive_ids),
                        PanelAccount.status.in_(
                            (PanelAccountStatus.active, PanelAccountStatus.error)
                        ),
                        Panel.worker_id.is_not(None),
                        Panel.is_active.is_(True),
                    )
                )
            ).all()
        seen: set[tuple[int, str]] = set()
        for _uid, pid, remote, rid, wid in rows:
            if not wid:
                continue
            name = (remote or "").strip() or (str(rid or "")).strip()
            if not name or name.startswith("{"):
                continue
            key = (int(pid), name.lower())
            if key in seen:
                continue
            seen.add(key)
            if enqueue_probe(
                worker_id=int(wid),
                panel_id=int(pid),
                remote_username=name,
                ttl=ttl,
                boost=True,  # /sub → front of RR
            ):
                n += 1
    except Exception:  # noqa: BLE001
        logger.debug(
            "enqueue_users_remotes failed n=%s", len(uids), exc_info=True
        )
    return n


def enqueue_near_limit_user(
    *,
    user_id: int,
    used: int,
    data_limit: int,
    remotes: list[tuple[int, int, str]],
    ttl: float = _DEFAULT_TTL,
) -> int:
    """Enqueue remotes when used ≥ 85% of data_limit. ``remotes`` = (wid, pid, name)."""
    lim = int(data_limit or 0)
    if lim <= 0:
        return 0
    if int(used or 0) < int(_NEAR_LIMIT_RATIO * lim):
        return 0
    n = 0
    for wid, pid, name in remotes:
        if enqueue_probe(
            worker_id=int(wid),
            panel_id=int(pid),
            remote_username=str(name),
            ttl=ttl,
            boost=True,
        ):
            n += 1
    return n


def queue_stats() -> dict[str, int]:
    now = time.monotonic()
    live = sum(1 for exp in _EXPIRE.values() if exp >= now)
    redis_n = -1
    try:
        from app.core.redis_cache import get_redis

        client = get_redis()
        if client is not None:
            redis_n = 0
            for key in client.scan_iter(match="astra:prio:*", count=50):
                try:
                    redis_n += int(client.zcard(key) or 0)
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
    return {
        "priority_queued": live if redis_n < 0 else redis_n,
        "priority_map": len(_QUEUE),
        "priority_redis": redis_n,
    }
