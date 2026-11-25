"""Fast-lane worker commands (KILL_USER) with Redis/in-memory long-poll wake."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

logger = logging.getLogger("astra.worker.cmd")
kill_logger = logging.getLogger("astra.kill")

# In-process fallback when Redis is down (per uvicorn worker).
_QUEUES: dict[int, list[dict[str, Any]]] = {}
_EVENTS: dict[int, asyncio.Event] = {}
_LOCK = asyncio.Lock()

# Dedup KILL_USER storms from /sub auto-refresh on already-limited users.
_KILL_DISPATCHED_UNTIL: dict[int, float] = {}
# Was 180s — partial freezes then blocked retries while remotes kept bleeding.
_KILL_COOLDOWN_SEC = 45.0


def _kill_cd_key(user_id: int) -> str:
    return f"astra:killcd:{int(user_id)}"


def kill_on_cooldown(user_id: int) -> bool:
    """True when a KILL_USER was dispatched for this user within the TTL.

    Prefers Redis so all uvicorn / astra-bg processes share one cooldown
    (in-memory alone re-fired every /sub hit that landed on another worker).
    """
    uid = int(user_id)
    try:
        from app.core.redis_cache import get_redis

        r = get_redis()
        if r is not None and r.exists(_kill_cd_key(uid)):
            return True
    except Exception:  # noqa: BLE001
        pass
    return time.monotonic() < float(
        _KILL_DISPATCHED_UNTIL.get(uid, 0.0) or 0.0
    )


def try_acquire_kill_slot(user_id: int, *, ttl: float | None = None) -> bool:
    """Atomically claim the kill slot. False → another process already killed.

    Uses Redis ``SET NX EX`` so concurrent /sub on different uvicorn workers
    cannot both dispatch. Falls back to in-memory TTL when Redis is down.
    """
    uid = int(user_id)
    sec = max(1, int(float(_KILL_COOLDOWN_SEC if ttl is None else ttl)))
    try:
        from app.core.redis_cache import get_redis

        r = get_redis()
        if r is not None:
            # SET NX returns True only for the first claim.
            got = r.set(_kill_cd_key(uid), "1", nx=True, ex=sec)
            if got:
                _KILL_DISPATCHED_UNTIL[uid] = time.monotonic() + float(sec)
                return True
            return False
    except Exception:  # noqa: BLE001
        pass
    if kill_on_cooldown(uid):
        return False
    _KILL_DISPATCHED_UNTIL[uid] = time.monotonic() + float(sec)
    if len(_KILL_DISPATCHED_UNTIL) > 5000:
        now = time.monotonic()
        dead = [k for k, until in _KILL_DISPATCHED_UNTIL.items() if until <= now]
        for k in dead[:2000]:
            _KILL_DISPATCHED_UNTIL.pop(k, None)
    return True


def note_kill_dispatched(user_id: int, *, ttl: float | None = None) -> None:
    """Mark cooldown without claiming (legacy); prefer ``try_acquire_kill_slot``."""
    try_acquire_kill_slot(user_id, ttl=ttl)


def clear_kill_cooldown(user_id: int) -> None:
    """Clear cooldown on renew / reset / reactivate so the next limit re-kills."""
    uid = int(user_id)
    _KILL_DISPATCHED_UNTIL.pop(uid, None)
    try:
        from app.core.redis_cache import get_redis

        r = get_redis()
        if r is not None:
            r.delete(_kill_cd_key(uid))
    except Exception:  # noqa: BLE001
        pass


def _event(wid: int) -> asyncio.Event:
    ev = _EVENTS.get(wid)
    if ev is None:
        ev = asyncio.Event()
        _EVENTS[wid] = ev
    return ev


def _redis_key(worker_id: int) -> str:
    return f"astra:wcmd:{int(worker_id)}"


def enqueue_command(worker_id: int, cmd: dict[str, Any]) -> str:
    """Push a command for a worker. Returns command id."""
    wid = int(worker_id)
    cid = str(cmd.get("id") or uuid.uuid4().hex[:16])
    payload = dict(cmd)
    payload["id"] = cid
    payload["enqueued_at"] = time.time()
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    pushed = False
    try:
        from app.core.redis_cache import get_redis

        r = get_redis()
        if r is not None:
            key = _redis_key(wid)
            r.lpush(key, raw)
            r.ltrim(key, 0, 499)
            r.expire(key, 300)
            pushed = True
    except Exception:  # noqa: BLE001
        pass
    # Always mirror in-process for local long-poll wakeups.
    q = _QUEUES.setdefault(wid, [])
    q.append(payload)
    if len(q) > 500:
        del q[:-400]
    _event(wid).set()
    if not pushed:
        logger.debug("cmd queued in-memory only worker=%s op=%s", wid, payload.get("op"))
    return cid


async def take_commands(
    worker_id: int, *, limit: int = 20, timeout: float = 15.0
) -> list[dict[str, Any]]:
    """Long-poll: wait up to ``timeout`` for commands, return up to ``limit``."""
    wid = int(worker_id)
    lim = max(1, min(50, int(limit)))
    deadline = time.monotonic() + max(0.05, float(timeout))

    # Fast path: Redis BRPOP loop (cross-process).
    try:
        from app.core.redis_cache import get_redis

        r = get_redis()
    except Exception:  # noqa: BLE001
        r = None

    out: list[dict[str, Any]] = []
    if r is not None:
        key = _redis_key(wid)
        while len(out) < lim and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                # BRPOP blocks up to 1s so we can return early if timeout small.
                item = await asyncio.to_thread(
                    r.brpop, key, max(1, min(1, int(remaining)))
                )
            except Exception:  # noqa: BLE001
                item = None
            if not item:
                # Also drain in-memory (same process enqueued).
                async with _LOCK:
                    local = _QUEUES.get(wid) or []
                    if local:
                        chunk = local[: lim - len(out)]
                        del local[: len(chunk)]
                        out.extend(chunk)
                        _event(wid).clear()
                        break
                continue
            _key, raw = item
            try:
                if isinstance(raw, (bytes, bytearray)):
                    out.append(json.loads(raw.decode("utf-8")))
                else:
                    out.append(json.loads(str(raw)))
            except Exception:  # noqa: BLE001
                continue
            # Non-blocking drain of remaining without waiting.
            while len(out) < lim:
                try:
                    nxt = r.rpop(key)
                except Exception:  # noqa: BLE001
                    break
                if not nxt:
                    break
                try:
                    if isinstance(nxt, (bytes, bytearray)):
                        out.append(json.loads(nxt.decode("utf-8")))
                    else:
                        out.append(json.loads(str(nxt)))
                except Exception:  # noqa: BLE001
                    continue
        return out

    # In-memory long-poll with Event.
    ev = _event(wid)
    while True:
        async with _LOCK:
            q = _QUEUES.get(wid) or []
            if q:
                chunk = q[:lim]
                del q[: len(chunk)]
                if not q:
                    ev.clear()
                return chunk
            ev.clear()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return []
        try:
            await asyncio.wait_for(ev.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            return []


async def enqueue_kill_user(
    user_id: int, *, force: bool = False
) -> dict[str, Any]:
    """Build per-worker KILL_USER commands from the user's panel accounts.

    Opens its own DB session. Groups accounts by the worker that currently
    holds the panel lease. Includes ``used_traffic`` so workers can clamp
    remote ``data_limit <= used`` (hard Xray cut).

    Already-limited users hit a 180s in-memory cooldown so /sub auto-refresh
    cannot re-dispatch a 32-panel kill every few seconds.
    """
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import set_committed_value

    from app.core.database import AsyncSessionLocal
    from app.models.panel import Panel
    from app.models.user import PanelAccount, PanelAccountStatus, User, UserStatus
    from app.services import worker_client as wc

    uid = int(user_id)
    if not force and not try_acquire_kill_slot(uid):
        kill_logger.debug(
            "KILL_USER skipped cooldown user_id=%s", uid
        )
        return {
            "ok": True,
            "skipped": 1,
            "reason": "cooldown",
            "workers": 0,
            "user_id": uid,
            "local_panels": [],
            "panels": 0,
        }
    by_worker: dict[int, list[dict[str, Any]]] = {}
    local_panel_ids: list[int] = []
    panel_ids_all: list[int] = []
    async with AsyncSessionLocal() as db:
        # Sequential loads only — sibling selectinload races asyncpg.
        user = (
            await db.execute(select(User).where(User.id == uid))
        ).scalar_one_or_none()
        if user is None:
            return {"ok": False, "reason": "missing", "workers": 0}
        if user.status not in (
            UserStatus.limited,
            UserStatus.expired,
            UserStatus.disabled,
        ):
            return {"ok": False, "reason": "not_limited", "workers": 0}
        accounts = list(
            (
                await db.execute(
                    select(PanelAccount).where(PanelAccount.user_id == uid)
                )
            )
            .scalars()
            .all()
        )
        panel_ids = list({int(a.panel_id) for a in accounts if a.panel_id})
        panels_by_id: dict[int, Panel] = {}
        if panel_ids:
            for p in (
                await db.execute(select(Panel).where(Panel.id.in_(panel_ids)))
            ).scalars().all():
                panels_by_id[int(p.id)] = p
        for acc in accounts:
            panel = panels_by_id.get(int(acc.panel_id))
            if panel is not None:
                set_committed_value(acc, "panel", panel)

        for acc in accounts:
            if acc.status not in (
                PanelAccountStatus.active,
                PanelAccountStatus.error,
            ):
                continue
            panel = panels_by_id.get(int(acc.panel_id))
            if panel is None:
                continue
            panel_ids_all.append(int(panel.id))
            wid = getattr(panel, "worker_id", None)
            if not wid:
                from app.services.worker_lease import lease_worker_for_panel

                wid = lease_worker_for_panel(int(acc.panel_id))
            name = (acc.remote_username or "").strip() or str(acc.remote_id or "")
            if not name or name.startswith("{"):
                continue
            if not wid:
                local_panel_ids.append(int(panel.id))
                continue
            by_worker.setdefault(int(wid), []).append(
                {
                    "panel_id": int(acc.panel_id),
                    "remote_username": name,
                    "remote_id": str(acc.remote_id or "") or None,
                    "account_id": int(acc.id),
                    "used_traffic": int(acc.used_traffic or 0),
                    "panel": wc.panel_snapshot(panel),
                }
            )

    enqueued = 0
    killed_account_ids: list[int] = []
    for wid, accounts in by_worker.items():
        clean = [a for a in accounts if a.get("panel")]
        if not clean:
            continue
        enqueue_command(
            wid,
            {
                "op": "KILL_USER",
                "priority": 0,
                "user_id": uid,
                "accounts": clean,
            },
        )
        enqueued += 1
        for a in clean:
            try:
                killed_account_ids.append(int(a["account_id"]))
            except Exception:  # noqa: BLE001
                pass

    # Optimistic freeze mark on ALL live accounts for this user so heal/watch
    # stop re-queueing while Worker/local freeze finish the hard cut.
    try:
        from sqlalchemy import update

        from app.services.panel_usage_bulk import QUOTA_FROZEN_MARK

        async with AsyncSessionLocal() as db2:
            await db2.execute(
                update(PanelAccount)
                .where(PanelAccount.user_id == uid)
                .where(
                    PanelAccount.status.in_(
                        (
                            PanelAccountStatus.active,
                            PanelAccountStatus.error,
                        )
                    )
                )
                .values(error_message=QUOTA_FROZEN_MARK)
            )
            await db2.commit()
    except Exception:  # noqa: BLE001
        logger.debug(
            "freeze-mark after kill dispatch failed user=%s",
            uid,
            exc_info=True,
        )

    # Slot already claimed at entry; on force refresh TTL unconditionally.
    if force:
        try:
            from app.core.redis_cache import get_redis

            r = get_redis()
            if r is not None:
                r.setex(_kill_cd_key(uid), int(_KILL_COOLDOWN_SEC), "1")
        except Exception:  # noqa: BLE001
            pass
        _KILL_DISPATCHED_UNTIL[uid] = time.monotonic() + float(_KILL_COOLDOWN_SEC)
    kill_logger.info(
        "KILL_USER dispatched user_id=%s panels=%s workers=%s local=%s",
        uid,
        len(dict.fromkeys(panel_ids_all)),
        enqueued,
        len(dict.fromkeys(local_panel_ids)),
    )
    return {
        "ok": True,
        "workers": enqueued,
        "user_id": uid,
        "local_panels": list(dict.fromkeys(local_panel_ids)),
        "panels": len(dict.fromkeys(panel_ids_all)),
    }
