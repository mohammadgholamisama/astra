"""Worker CRUD, token auth, usage-report apply, heartbeat."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.adapters.http_utils import normalize_panel_base_url
from app.core.config import get_settings
from app.models.panel import Panel
from app.models.worker import WorkerNode
from app.schemas.workers import (
    WorkerCreate,
    WorkerOut,
    WorkerPanelBrief,
    WorkerUpdate,
    WorkerUsageReport,
)
from app.services import worker_client as wc

logger = logging.getLogger("astra.worker")

# ---- usage-report coalesce queue ----
# Worker sends many 1500-meter chunks; merging per panel avoids reloading all
# panel_accounts once per chunk (was pegging both uvicorn workers at ~100%).
_REPORT_WAKE: asyncio.Event | None = None
_REPORT_TASK: asyncio.Task | None = None
_REPORT_PENDING: dict[int, dict[str, Any]] = {}
_REPORT_LOCK = asyncio.Lock()
_REPORT_DIRTY: set[int] = set()
_REPORT_DIRTY_LOCK = asyncio.Lock()
_REPORT_STATS = {"queued": 0, "applied": 0, "dropped": 0, "errors": 0, "merged": 0}
# Outside PrivateTmp so all API workers share one flock.
_APPLY_LOCK_PATH = "/var/lib/astra/usage-apply.lock"
_PANEL_APPLY_COOLDOWN = float(
    __import__("os").environ.get("ASTRA_USAGE_APPLY_COOLDOWN", "18") or 18
)
_LOCAL_APPLY_TS: dict[int, float] = {}


def _report_wake() -> asyncio.Event:
    global _REPORT_WAKE
    if _REPORT_WAKE is None:
        _REPORT_WAKE = asyncio.Event()
    return _REPORT_WAKE


async def ensure_usage_report_worker() -> None:
    """Start background consumer once (call from lifespan)."""
    global _REPORT_TASK
    if _REPORT_TASK is not None and not _REPORT_TASK.done():
        await ensure_dirty_recount_worker()
        return
    _REPORT_TASK = asyncio.create_task(
        _usage_report_consumer(), name="worker-usage-report-q"
    )
    await ensure_dirty_recount_worker()


async def enqueue_usage_report(
    *,
    worker_id: int,
    panel_id: int,
    usage_map: dict[str, int],
    listed: int = 0,
    dt_ms: int | None = None,
    allow_shrink: bool = False,
    full_sync: bool = False,
    mode: str | None = None,
) -> dict[str, Any]:
    """Merge meters into per-panel pending map (O(1) accept, deferred apply)."""
    await ensure_usage_report_worker()
    pid = int(panel_id)
    async with _REPORT_LOCK:
        row = _REPORT_PENDING.get(pid)
        if row is None:
            if len(_REPORT_PENDING) >= 120:
                # Drop an arbitrary oldest non-priority panel to keep master responsive.
                drop_pid = next(
                    (
                        p
                        for p, r in _REPORT_PENDING.items()
                        if not r.get("priority")
                    ),
                    next(iter(_REPORT_PENDING)),
                )
                _REPORT_PENDING.pop(drop_pid, None)
                _REPORT_STATS["dropped"] += 1
            row = {
                "worker_id": int(worker_id),
                "usage_map": {},
                "listed": 0,
                "allow_shrink": False,
                "full_sync": False,
                "mode": None,
                "enqueued_at": time.monotonic(),
            }
            _REPORT_PENDING[pid] = row
            _REPORT_STATS["queued"] += 1
        else:
            _REPORT_STATS["merged"] += 1
        umap: dict[str, int] = row["usage_map"]
        shrink = bool(row.get("allow_shrink")) or bool(allow_shrink)
        for k, v in (usage_map or {}).items():
            name = str(k).strip()
            if not name:
                continue
            nv = max(0, int(v or 0))
            # Last write wins so remote resets reach high-water apply intact.
            umap[name] = nv
        row["allow_shrink"] = shrink
        row["worker_id"] = int(worker_id)
        row["listed"] = max(int(row.get("listed") or 0), int(listed or 0))
        row["full_sync"] = bool(row.get("full_sync")) or bool(full_sync)
        if mode:
            row["mode"] = str(mode)
        # Tiny maps = priority-probe; drain them ahead of bulk waves.
        if len(usage_map or {}) <= 120:
            row["priority"] = True
        meters_n = len(umap)
        pending_n = len(_REPORT_PENDING)
    _report_wake().set()
    return {
        "queued": True,
        "merged": True,
        "qsize": pending_n,
        "meters": meters_n,
    }


async def _usage_report_consumer() -> None:
    from app.core.database import pool_pressure_blocks_background

    wake = _report_wake()
    while True:
        await wake.wait()
        wake.clear()
        while True:
            async with _REPORT_LOCK:
                if not _REPORT_PENDING:
                    break
                # Prefer priority-probe (tiny) maps, then largest bulk maps.
                def _rank(p: int) -> tuple[int, int]:
                    r = _REPORT_PENDING[p]
                    n = len(r.get("usage_map") or {})
                    pri = 1 if r.get("priority") or n <= 120 else 0
                    return (pri, -n if pri else n)

                pid = max(_REPORT_PENDING.keys(), key=_rank)
                row = _REPORT_PENDING.pop(pid)
            job = {
                "panel_id": int(pid),
                "worker_id": int(row["worker_id"]),
                "usage_map": row.get("usage_map") or {},
                "listed": int(row.get("listed") or 0),
                "allow_shrink": bool(row.get("allow_shrink")),
                "full_sync": bool(row.get("full_sync")),
                "mode": row.get("mode"),
                "priority": bool(row.get("priority"))
                or len(row.get("usage_map") or {}) <= 120,
            }
            try:
                if pool_pressure_blocks_background():
                    await asyncio.sleep(2.0)
                await _apply_usage_report_job(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _REPORT_STATS["errors"] += 1
                logger.exception(
                    "usage-report job failed panel=%s", job.get("panel_id")
                )
            # Let API / create breathe between heavy applies.
            await asyncio.sleep(0.35)


def _try_acquire_apply_lock():
    """Cross-process lock so two uvicorn workers don't apply at once."""
    import fcntl
    from pathlib import Path

    try:
        Path(_APPLY_LOCK_PATH).parent.mkdir(parents=True, exist_ok=True)
        fd = open(_APPLY_LOCK_PATH, "w", encoding="utf-8")
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        return None
    except Exception:  # noqa: BLE001
        return None


def _panel_apply_deferred(pid: int, *, force: bool = False) -> bool:
    """True if another worker applied this panel within the cooldown window."""
    # Full-sync may shrink meters — shorter cool, never zero (avoids stampede).
    cool = 6.0 if force else max(8.0, float(_PANEL_APPLY_COOLDOWN))
    now = time.monotonic()
    last = float(_LOCAL_APPLY_TS.get(pid, 0.0) or 0.0)
    if now - last < cool:
        return True
    try:
        from app.core.redis_cache import get_redis

        client = get_redis()
        if client is not None:
            # SET NX — first worker wins the window; others coalesce.
            ok = client.set(
                f"astra:usage:apply:{int(pid)}",
                b"1",
                nx=True,
                ex=max(3, int(cool)),
            )
            if not ok:
                return True
    except Exception:  # noqa: BLE001
        pass
    _LOCAL_APPLY_TS[pid] = now
    return False


def _requeue_usage_job(
    pid: int,
    wid: int,
    usage_map: dict[str, int],
    *,
    allow_shrink: bool = False,
    full_sync: bool = False,
    mode: str | None = None,
    priority: bool = False,
) -> None:
    """Merge meters back into the pending coalesce map."""
    # Called under/without lock — take the async lock at call sites when needed.
    row = _REPORT_PENDING.get(pid) or {
        "worker_id": wid,
        "usage_map": {},
        "listed": 0,
        "allow_shrink": False,
        "full_sync": False,
        "mode": None,
        "enqueued_at": time.monotonic(),
    }
    umap: dict[str, int] = row["usage_map"]
    shrink = bool(row.get("allow_shrink")) or bool(allow_shrink)
    for k, v in usage_map.items():
        name = str(k)
        nv = max(0, int(v or 0))
        # Last write wins — high-water apply detects remote resets when got drops.
        umap[name] = nv
    row["usage_map"] = umap
    row["worker_id"] = wid
    row["allow_shrink"] = shrink
    row["full_sync"] = bool(row.get("full_sync")) or bool(full_sync)
    if mode:
        row["mode"] = str(mode)
    if priority:
        row["priority"] = True
    _REPORT_PENDING[pid] = row


async def _apply_usage_report_job(job: dict[str, Any]) -> None:
    from app.core.database import AsyncSessionLocal
    from app.services.panel_usage_bulk import apply_usage_map_to_panel

    pid = int(job["panel_id"])
    wid = int(job["worker_id"])
    usage_map: dict[str, int] = job.get("usage_map") or {}
    allow_shrink = bool(job.get("allow_shrink"))
    priority = bool(job.get("priority")) or len(usage_map) <= 120
    if not usage_map:
        return
    mode = str(job.get("mode") or "").strip().lower()
    full_sync = bool(job.get("full_sync"))
    # Per-panel cooldown — skip for priority-probe (≤120 meters) so /sub stays live.
    # Cold/full_sync waves use shorter cool so last_sync stamps keep moving.
    if not priority and _panel_apply_deferred(
        pid, force=allow_shrink or full_sync or mode == "cold"
    ):
        async with _REPORT_LOCK:
            _requeue_usage_job(
                pid,
                wid,
                usage_map,
                allow_shrink=allow_shrink,
                full_sync=full_sync,
                mode=mode or None,
                priority=priority,
            )
        await asyncio.sleep(1.0)
        _report_wake().set()
        return
    # Serialize applies across uvicorn workers (shared CPU/DB).
    lock_fd = await asyncio.to_thread(_try_acquire_apply_lock)
    if lock_fd is None:
        async with _REPORT_LOCK:
            _requeue_usage_job(
                pid,
                wid,
                usage_map,
                allow_shrink=allow_shrink,
                full_sync=full_sync,
                mode=mode or None,
                priority=priority,
            )
        await asyncio.sleep(0.8)
        _report_wake().set()
        return
    t0 = time.monotonic()
    dirty: set[int] = set()
    exhausted: list[int] = []
    near_limit_uids: list[int] = []
    try:
        async with AsyncSessionLocal() as db:
            panel = (
                await db.execute(select(Panel.id).where(Panel.id == pid))
            ).scalar_one_or_none()
            if panel is None:
                return
            dirty = await apply_usage_map_to_panel(
                db,
                pid,
                usage_map,
                allow_shrink=allow_shrink,
            )
            # Bulk / cold / full_sync waves prove the panel was scanned — stamp
            # last_sync_at on remaining live remotes so fleet freshness recovers
            # even when most meters were unchanged (delta-only payloads).
            # Threshold 80: cache sync-loop posts ~1200; hot probes stay small.
            bulk_prove = (
                full_sync
                or mode == "cold"
                or len(usage_map) >= 80
            )
            if bulk_prove:
                try:
                    from datetime import datetime, timedelta, timezone

                    from sqlalchemy import text

                    now_ts = datetime.now(timezone.utc)
                    cutoff = now_ts - timedelta(minutes=5)
                    res_stamp = await db.execute(
                        text(
                            """
                            UPDATE panel_accounts
                            SET last_sync_at = :now
                            WHERE panel_id = :pid
                              AND status IN ('active', 'error', 'pending')
                              AND coalesce(remote_username, '') <> ''
                              AND (
                                    last_sync_at IS NULL
                                 OR last_sync_at < :cutoff
                              )
                            """
                        ),
                        {
                            "now": now_ts,
                            "cutoff": cutoff,
                            "pid": int(pid),
                        },
                    )
                    try:
                        stamped = int(getattr(res_stamp, "rowcount", 0) or 0)
                    except Exception:  # noqa: BLE001
                        stamped = 0
                    if stamped:
                        logger.info(
                            "bulk last_sync stamp panel=%s rows=%s mode=%s meters=%s",
                            pid,
                            stamped,
                            mode or "-",
                            len(usage_map),
                        )
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "cold last_sync stamp soft-fail panel=%s", pid, exc_info=True
                    )
            # Instant quota flip for users whose weighted total just crossed limit.
            if dirty:
                from app.models.user import User, UserStatus
                from app.services.quota import is_quota_exhausted

                urows = (
                    await db.execute(
                        select(
                            User.id,
                            User.used_traffic,
                            User.data_limit,
                            User.status,
                        ).where(User.id.in_(list(dirty)))
                    )
                ).all()
                for uid, used, lim, st in urows:
                    used_i = int(used or 0)
                    lim_i = int(lim or 0)
                    if st == UserStatus.active and is_quota_exhausted(used_i, lim_i):
                        user_obj = await db.get(User, int(uid))
                        if user_obj is not None and user_obj.status == UserStatus.active:
                            user_obj.status = UserStatus.limited
                            exhausted.append(int(uid))
                    elif (
                        st == UserStatus.active
                        and lim_i > 0
                        and used_i >= int(0.85 * lim_i)
                    ):
                        near_limit_uids.append(int(uid))
            worker = (
                await db.execute(select(WorkerNode).where(WorkerNode.id == wid))
            ).scalar_one_or_none()
            if worker is not None:
                worker.last_seen_at = datetime.now(timezone.utc)
                worker.status = "online"
                worker.last_error = None
            await db.commit()
        _LOCAL_APPLY_TS[pid] = time.monotonic()
        # Near-limit + dirty → Hot via ONE bulk session (not N opens).
        from app.services import usage_priority as uprio

        promote_ids = list(
            dict.fromkeys(
                [int(x) for x in near_limit_uids[:80]]
                + [int(x) for x in list(dirty)[:120]]
            )
        )
        if promote_ids:
            try:
                # dirty → 10 min Hot; near-limit share the same bulk enqueue.
                await uprio.enqueue_users_remotes(promote_ids, ttl=600.0)
            except Exception:  # noqa: BLE001
                pass
    finally:
        try:
            import fcntl

            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            lock_fd.close()
        except Exception:  # noqa: BLE001
            pass
    # Instant multi-panel cut — Worker KILL_USER + local freeze together.
    if exhausted:
        from app.services.orchestrator import kick_freeze_user

        for uid in exhausted[:80]:
            try:
                await kick_freeze_user(int(uid), remote_timeout=8.0, force=True)
            except Exception:  # noqa: BLE001
                pass
    if dirty:
        async with _REPORT_DIRTY_LOCK:
            _REPORT_DIRTY.update(int(x) for x in dirty)
            if len(_REPORT_DIRTY) > 2500:
                keep = list(_REPORT_DIRTY)[:2000]
                _REPORT_DIRTY.clear()
                _REPORT_DIRTY.update(keep)
        _dirty_recount_wake().set()
    _REPORT_STATS["applied"] += 1
    wc.mark_worker_health(wid, True, "")
    async with _REPORT_LOCK:
        pending_n = len(_REPORT_PENDING)
    logger.info(
        "usage-report applied panel=%s meters=%s dirty=%s exhausted=%s dt=%.2fs pending=%s",
        pid,
        len(usage_map),
        len(dirty),
        len(exhausted),
        time.monotonic() - t0,
        pending_n,
    )


# ---- batched user.used_traffic recount (every ~10s) ----
_DIRTY_RECOUNT_TASK: asyncio.Task | None = None
_DIRTY_RECOUNT_WAKE: asyncio.Event | None = None


def _dirty_recount_wake() -> asyncio.Event:
    global _DIRTY_RECOUNT_WAKE
    if _DIRTY_RECOUNT_WAKE is None:
        _DIRTY_RECOUNT_WAKE = asyncio.Event()
    return _DIRTY_RECOUNT_WAKE


async def ensure_dirty_recount_worker() -> None:
    """Flush dirty user totals in batches so meters stay accurate without stampede."""
    global _DIRTY_RECOUNT_TASK
    if _DIRTY_RECOUNT_TASK is not None and not _DIRTY_RECOUNT_TASK.done():
        return
    _DIRTY_RECOUNT_TASK = asyncio.create_task(
        _dirty_recount_consumer(), name="worker-dirty-recount"
    )


async def _dirty_recount_consumer() -> None:
    from app.services.panel_usage_bulk import recount_users_usage

    wake = _dirty_recount_wake()
    while True:
        try:
            try:
                await asyncio.wait_for(wake.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                pass
            wake.clear()
            ids = await drain_report_dirty_for_recount(limit=250)
            if not ids:
                continue
            t0 = time.monotonic()
            # Isolated sessions per user — never share one AsyncSession across
            # concurrent recount work (asyncpg InternalClientError).
            flipped = await recount_users_usage(ids, concurrency=8)
            # Instant freeze for users who just hit quota — own sessions.
            if flipped:
                from app.services.orchestrator import kick_freeze_user

                for uid in flipped[:40]:
                    try:
                        await kick_freeze_user(int(uid), remote_timeout=8.0)
                    except Exception:  # noqa: BLE001
                        pass
            logger.info(
                "dirty recount n=%s flipped=%s dt=%.2fs left=%s",
                len(ids),
                len(flipped),
                time.monotonic() - t0,
                len(_REPORT_DIRTY),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("dirty recount failed")
            await asyncio.sleep(2.0)


async def drain_report_dirty_for_recount(limit: int = 400) -> list[int]:
    """Take pending dirty user ids left by usage-report jobs (for lagging loop)."""
    async with _REPORT_DIRTY_LOCK:
        if not _REPORT_DIRTY:
            return []
        out = list(_REPORT_DIRTY)[: max(1, int(limit))]
        for uid in out:
            _REPORT_DIRTY.discard(uid)
        return out


def usage_report_queue_stats() -> dict[str, int]:
    return {
        **{k: int(v) for k, v in _REPORT_STATS.items()},
        "qsize": len(_REPORT_PENDING),
        "pending_dirty": len(_REPORT_DIRTY),
    }


def _master_public_url() -> str:
    settings = get_settings()
    base = (
        getattr(settings, "PANEL_BASE_URL", None)
        or getattr(settings, "PUBLIC_BASE_URL", None)
        or ""
    )
    if not base or "localhost" in base:
        try:
            from app.services.site_settings import get_panel_base_url

            base = get_panel_base_url() or base
        except Exception:  # noqa: BLE001
            pass
    return str(base or "").rstrip("/")


def install_command(worker: WorkerNode, token: str) -> str:
    master = _master_public_url() or "https://YOUR-PANEL-HOST"
    port = int(worker.listen_port or 2090)
    return (
        f"curl -fsSL '{master}/api/public/worker-install.sh' "
        f"| bash -s -- --master '{master}' --token '{token}' "
        f"--port {port} --name '{worker.name}'"
    )


def to_out(
    worker: WorkerNode,
    *,
    token: str | None = None,
    include_install: bool = False,
    install_ok: bool | None = None,
    install_log: str | None = None,
    message: str | None = None,
) -> WorkerOut:
    panels = list(getattr(worker, "panels", None) or [])
    briefs = [
        WorkerPanelBrief(
            id=int(p.id),
            name=str(p.name),
            type=str(getattr(p.type, "value", p.type)),
            is_active=bool(p.is_active),
        )
        for p in panels
    ]
    tok = token or (wc.peek_raw_token(int(worker.id)) if include_install else None)
    meta = dict(getattr(worker, "meta", None) or {})
    if install_ok is None and "install_ok" in meta:
        install_ok = bool(meta.get("install_ok"))
    if install_log is None and meta.get("install_log"):
        install_log = str(meta.get("install_log"))
    if message is None and meta.get("installing") and worker.status == "unknown":
        message = "در حال نصب…"
    return WorkerOut(
        id=int(worker.id),
        name=str(worker.name),
        base_url=str(worker.base_url),
        is_active=bool(worker.is_active),
        status=str(worker.status or "unknown"),
        last_seen_at=worker.last_seen_at,
        last_error=worker.last_error,
        ssh_host=worker.ssh_host,
        ssh_user=worker.ssh_user,
        listen_port=int(worker.listen_port or 2090),
        panels=briefs,
        panel_ids=[int(p.id) for p in panels],
        created_at=worker.created_at,
        token=tok,
        install_command=(
            install_command(worker, tok) if tok and include_install else None
        ),
        install_ok=install_ok,
        install_log=install_log,
        message=message,
    )


async def list_workers(db: AsyncSession) -> list[WorkerOut]:
    rows = (
        await db.execute(
            select(WorkerNode)
            .options(selectinload(WorkerNode.panels))
            .order_by(WorkerNode.id.asc())
        )
    ).scalars().all()
    return [to_out(w) for w in rows]


async def get_worker(db: AsyncSession, worker_id: int) -> WorkerNode | None:
    return (
        await db.execute(
            select(WorkerNode)
            .where(WorkerNode.id == int(worker_id))
            .options(selectinload(WorkerNode.panels))
        )
    ).scalar_one_or_none()


async def _set_panel_ids(
    db: AsyncSession, worker_id: int, panel_ids: list[int]
) -> None:
    """Assign panels to a worker as both preferred affinity and active lease."""
    want = {int(x) for x in panel_ids}
    wid = int(worker_id)
    # Clear preferred+lease for panels this worker no longer owns as preferred.
    current_pref = (
        await db.execute(
            select(Panel.id).where(Panel.preferred_worker_id == wid)
        )
    ).scalars().all()
    drop_pref = [int(i) for i in current_pref if int(i) not in want]
    if drop_pref:
        await db.execute(
            update(Panel)
            .where(Panel.id.in_(drop_pref))
            .values(preferred_worker_id=None)
        )
    current = (
        await db.execute(
            select(Panel.id).where(Panel.worker_id == wid)
        )
    ).scalars().all()
    drop = [int(i) for i in current if int(i) not in want]
    if drop:
        await db.execute(
            update(Panel).where(Panel.id.in_(drop)).values(worker_id=None)
        )
    if want:
        await db.execute(
            update(Panel)
            .where(Panel.id.in_(want))
            .values(worker_id=wid, preferred_worker_id=wid)
        )


async def create_worker(db: AsyncSession, body: WorkerCreate) -> WorkerOut:
    from app.services.worker_install import (
        _sanitize_host,
        ensure_sshpass_installed,
        ssh_install_worker,
    )

    name = (body.name or "").strip()
    if not name:
        raise ValueError("نام سرور الزامی است")
    host = _sanitize_host(body.ssh_host or "")
    if not host:
        raise ValueError("IP یا آدرس سرور الزامی است")
    if not (body.ssh_password or "").strip():
        raise ValueError("رمز SSH الزامی است")
    exists = (
        await db.execute(select(WorkerNode.id).where(WorkerNode.name == name))
    ).scalar_one_or_none()
    if exists:
        raise ValueError("نام سرور تکراری است")

    token = wc.new_worker_token()
    port = int(body.listen_port or 2090)
    user = (body.ssh_user or "root").strip() or "root"
    if body.base_url and body.base_url.strip():
        base = normalize_panel_base_url(body.base_url.strip())
    else:
        base = normalize_panel_base_url(f"http://{host}:{port}")

    worker = WorkerNode(
        name=name,
        base_url=base,
        token_hash=wc.hash_worker_token(token),
        is_active=True,
        status="unknown",
        ssh_host=host,
        ssh_user=user,
        listen_port=port,
        meta={"agent_token": token},
    )
    db.add(worker)
    await db.flush()
    if body.panel_ids:
        await _set_panel_ids(db, int(worker.id), list(body.panel_ids))
    await db.commit()
    wc.register_worker_route(int(worker.id), base, token, active=True)

    # Install in background so Cloudflare / UI don't time out (2–5 min apt+pip).
    install_ok: bool | None = None
    install_log: str | None = None
    message: str | None = None
    wid = int(worker.id)

    if body.auto_install:
        master = _master_public_url()
        if not master or "localhost" in master:
            message = "آدرس پنل اصلی نامعتبر است — نصب خودکار ممکن نیست"
            install_ok = False
            install_log = message
        else:
            import asyncio

            pwd = body.ssh_password
            ssh_port = int(body.ssh_port or 22)

            async def _bg_install() -> None:
                from app.core.database import AsyncSessionLocal

                try:
                    await ensure_sshpass_installed()
                    result = await ssh_install_worker(
                        host=host,
                        username=user,
                        password=pwd,
                        master_url=master,
                        token=token,
                        listen_port=port,
                        name=name,
                        ssh_port=ssh_port,
                    )
                    async with AsyncSessionLocal() as s:
                        wrow = await get_worker(s, wid)
                        if wrow is None:
                            return
                        if result.base_url:
                            wrow.base_url = normalize_panel_base_url(
                                result.base_url
                            )
                        meta = dict(wrow.meta or {})
                        meta["install_log"] = (result.log or "")[-3500:]
                        meta["install_ok"] = bool(result.ok)
                        wrow.meta = meta
                        if result.ok:
                            wrow.status = "online"
                            wrow.last_error = None
                        else:
                            wrow.status = "offline"
                            wrow.last_error = (result.log or "")[:400]
                        await s.commit()
                        wc.register_worker_route(
                            wid,
                            str(wrow.base_url),
                            token,
                            active=True,
                        )
                    if result.ok:
                        async with AsyncSessionLocal() as s2:
                            try:
                                await sync_assignments_to_worker(s2, wid)
                            except Exception:  # noqa: BLE001
                                pass
                            try:
                                await probe_all_workers(s2)
                            except Exception:  # noqa: BLE001
                                pass
                    logger.info(
                        "worker auto-install done id=%s ok=%s",
                        wid,
                        result.ok,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("worker auto-install failed id=%s", wid)
                    try:
                        async with AsyncSessionLocal() as s3:
                            wrow = await get_worker(s3, wid)
                            if wrow is not None:
                                wrow.status = "offline"
                                wrow.last_error = "auto-install crashed"
                                meta = dict(wrow.meta or {})
                                meta["install_ok"] = False
                                wrow.meta = meta
                                await s3.commit()
                    except Exception:  # noqa: BLE001
                        pass

            # Mark installing
            worker.status = "unknown"
            meta = dict(worker.meta or {})
            meta["installing"] = True
            worker.meta = meta
            await db.commit()
            asyncio.create_task(_bg_install())
            message = (
                "در حال نصب خودکار روی سرور… وضعیت تا ۱–۳ دقیقه دیگر "
                "به آنلاین/قطع به‌روز می‌شود"
            )
            install_ok = None
            install_log = "installing…"

    fresh = await get_worker(db, wid)
    assert fresh is not None
    return to_out(
        fresh,
        token=token,
        include_install=True,
        install_ok=install_ok,
        install_log=install_log,
        message=message,
    )


async def reinstall_worker(
    db: AsyncSession,
    worker_id: int,
    *,
    ssh_password: str,
    ssh_port: int | None = None,
) -> WorkerOut:
    from app.services.worker_install import ensure_sshpass_installed, ssh_install_worker

    worker = await get_worker(db, worker_id)
    if worker is None:
        raise LookupError("worker not found")
    host = (worker.ssh_host or "").strip()
    if not host:
        raise ValueError("برای این Worker آدرس SSH ثبت نشده")
    token = str((worker.meta or {}).get("agent_token") or "") or wc.peek_raw_token(
        int(worker.id)
    )
    if not token:
        # rotate implicitly
        rotated = await rotate_token(db, worker_id)
        token = rotated.token or ""
        worker = await get_worker(db, worker_id)
        assert worker is not None
    master = _master_public_url()
    if not master:
        raise ValueError("آدرس پنل اصلی مشخص نیست")
    await ensure_sshpass_installed()
    result = await ssh_install_worker(
        host=host,
        username=(worker.ssh_user or "root"),
        password=ssh_password,
        master_url=master,
        token=str(token),
        listen_port=int(worker.listen_port or 2090),
        name=str(worker.name),
        ssh_port=int(ssh_port or 22),
    )
    if result.base_url:
        worker.base_url = normalize_panel_base_url(result.base_url)
    worker.status = "online" if result.ok else "offline"
    worker.last_error = None if result.ok else (result.log or "")[:400]
    await db.commit()
    if token:
        wc.register_worker_route(
            int(worker.id),
            str(worker.base_url),
            str(token),
            active=True,
        )
    if result.ok:
        try:
            await sync_assignments_to_worker(db, int(worker.id))
        except Exception:  # noqa: BLE001
            pass
    fresh = await get_worker(db, worker_id)
    assert fresh is not None
    return to_out(
        fresh,
        token=str(token) if result.ok else None,
        include_install=True,
        install_ok=bool(result.ok),
        install_log=result.log,
        message=(
            "نصب مجدد موفق بود"
            if result.ok
            else "نصب مجدد ناموفق — لاگ را بررسی کنید"
        ),
    )


async def update_worker(
    db: AsyncSession, worker_id: int, body: WorkerUpdate
) -> WorkerOut:
    worker = await get_worker(db, worker_id)
    if worker is None:
        raise LookupError("worker not found")
    if body.name is not None:
        name = body.name.strip()
        if name and name != worker.name:
            clash = (
                await db.execute(
                    select(WorkerNode.id).where(
                        WorkerNode.name == name, WorkerNode.id != int(worker_id)
                    )
                )
            ).scalar_one_or_none()
            if clash:
                raise ValueError("نام سرور تکراری است")
            worker.name = name
    if body.base_url is not None:
        worker.base_url = normalize_panel_base_url(body.base_url.strip())
    if body.listen_port is not None:
        worker.listen_port = int(body.listen_port)
    if body.is_active is not None:
        worker.is_active = bool(body.is_active)
    if body.clear_ssh_host:
        worker.ssh_host = None
    elif body.ssh_host is not None:
        worker.ssh_host = body.ssh_host.strip() or None
    if body.clear_ssh_user:
        worker.ssh_user = None
    elif body.ssh_user is not None:
        worker.ssh_user = body.ssh_user.strip() or None
    if body.panel_ids is not None:
        await _set_panel_ids(db, int(worker_id), list(body.panel_ids))
    await db.commit()
    fresh = await get_worker(db, worker_id)
    assert fresh is not None
    tok = str((fresh.meta or {}).get("agent_token") or "") or wc.peek_raw_token(
        int(fresh.id)
    )
    if tok:
        wc.register_worker_route(
            int(fresh.id),
            str(fresh.base_url),
            tok,
            active=bool(fresh.is_active),
        )
    return to_out(fresh)


async def rotate_token(db: AsyncSession, worker_id: int) -> WorkerOut:
    worker = await get_worker(db, worker_id)
    if worker is None:
        raise LookupError("worker not found")
    token = wc.new_worker_token()
    worker.token_hash = wc.hash_worker_token(token)
    worker.status = "unknown"
    meta = dict(worker.meta or {})
    meta["agent_token"] = token
    worker.meta = meta
    await db.commit()
    wc.register_worker_route(
        int(worker.id), str(worker.base_url), token, active=True
    )
    fresh = await get_worker(db, worker_id)
    assert fresh is not None
    return to_out(fresh, token=token, include_install=True)


async def delete_worker(db: AsyncSession, worker_id: int) -> None:
    worker = await get_worker(db, worker_id)
    if worker is None:
        raise LookupError("worker not found")
    await db.execute(
        update(Panel).where(Panel.worker_id == int(worker_id)).values(worker_id=None)
    )
    await db.execute(
        update(Panel)
        .where(Panel.preferred_worker_id == int(worker_id))
        .values(preferred_worker_id=None)
    )
    await db.delete(worker)
    await db.commit()
    wc.drop_worker_route(int(worker_id))


async def authenticate_worker_token(
    db: AsyncSession, token: str
) -> WorkerNode | None:
    th = wc.hash_worker_token(token)
    return (
        await db.execute(
            select(WorkerNode).where(
                WorkerNode.token_hash == th, WorkerNode.is_active.is_(True)
            )
        )
    ).scalar_one_or_none()


async def apply_usage_report(
    db: AsyncSession, worker: WorkerNode, report: WorkerUsageReport
) -> dict[str, Any]:
    """Accept meters from worker and queue apply (non-blocking for master CPU).

    Previously applied + recounted inline → 500s under concurrent 20×32k maps.
    Now: validate panel, touch heartbeat lightly, enqueue, return immediately.
    """
    pid = int(report.panel_id)
    panel = (
        await db.execute(select(Panel.id, Panel.worker_id).where(Panel.id == pid))
    ).one_or_none()
    if panel is None:
        raise LookupError("panel not found")
    _panel_id, assigned_wid = panel[0], panel[1]
    if assigned_wid and int(assigned_wid) != int(worker.id):
        logger.warning(
            "usage report panel=%s from worker=%s assigned=%s",
            pid,
            worker.id,
            assigned_wid,
        )
    usage_map: dict[str, int] = {}
    for m in report.meters or []:
        name = (m.remote_username or "").strip()
        if not name:
            continue
        usage_map[name] = max(0, int(m.used_traffic or 0))
    # Heartbeat only — heavy apply is queued.
    worker.last_seen_at = datetime.now(timezone.utc)
    worker.status = "online"
    worker.last_error = None
    await db.commit()
    wc.mark_worker_health(int(worker.id), True, "")
    try:
        from app.services.worker_lease import note_heartbeat, refresh_leases_for_worker

        note_heartbeat(int(worker.id))
        refresh_leases_for_worker(int(worker.id))
    except Exception:  # noqa: BLE001
        pass
    if not usage_map:
        return {"dirty": 0, "listed": 0, "queued": False}
    # High-water mark handles remote resets; never shrink accumulated raw
    # just because full_sync reported a lower remote counter.
    qinfo = await enqueue_usage_report(
        worker_id=int(worker.id),
        panel_id=pid,
        usage_map=usage_map,
        listed=int(report.listed or len(usage_map)),
        dt_ms=report.dt_ms,
        allow_shrink=False,
        full_sync=bool(getattr(report, "full_sync", False)),
        mode=getattr(report, "mode", None),
    )
    # Hot reports from priority-probe must jump the apply queue.
    try:
        if str(getattr(report, "mode", None) or "").lower() == "hot":
            async with _REPORT_LOCK:
                row = _REPORT_PENDING.get(pid)
                if row is not None:
                    row["priority"] = True
    except Exception:  # noqa: BLE001
        pass
    return {
        "dirty": 0,
        "listed": len(usage_map),
        "queued": bool(qinfo.get("queued")),
        "qsize": int(qinfo.get("qsize") or 0),
        "dropped": bool(qinfo.get("dropped") or qinfo.get("dropped_oldest")),
    }


async def touch_heartbeat(
    db: AsyncSession, worker: WorkerNode, *, version: str | None = None
) -> None:
    worker.last_seen_at = datetime.now(timezone.utc)
    worker.status = "online"
    worker.last_error = None
    if version:
        meta = dict(worker.meta or {})
        meta["version"] = version
        worker.meta = meta
    await db.commit()
    wc.mark_worker_health(int(worker.id), True, "")
    try:
        from app.services.worker_lease import note_heartbeat, refresh_leases_for_worker

        note_heartbeat(int(worker.id))
        refresh_leases_for_worker(int(worker.id))
    except Exception:  # noqa: BLE001
        pass


async def assignment_payload_for_worker(
    db: AsyncSession, worker: WorkerNode
) -> dict[str, Any]:
    """Exact leased panels + tier hints for this worker (zero duplicate work)."""
    from app.models.panel import PanelType
    from app.services import usage_tiers as tiers

    panels = []
    tier_map: dict[str, dict[str, list[str]]] = {}
    for p in worker.panels or []:
        if not p.is_active:
            continue
        if getattr(p, "type", None) == PanelType.astra:
            continue
        # Strict affinity: only panels whose active lease is this worker.
        if int(getattr(p, "worker_id", 0) or 0) != int(worker.id):
            continue
        snap = wc.panel_snapshot(p)
        panels.append(snap)
        t = tiers.panel_tier_names(int(p.id))
        if t["hot"] or t["warm"]:
            tier_map[str(int(p.id))] = t
    return {
        "ok": True,
        "worker_id": int(worker.id),
        "interval_sec": 20,
        "cold_every_sec": 60,
        "panels": panels,
        "tiers": tier_map,
    }


async def resolve_worker_route(
    db: AsyncSession, panel: Panel
) -> tuple[WorkerNode, str] | None:
    """Return (worker, raw_token) if panel should route remotely and we have a token."""
    wid = getattr(panel, "worker_id", None)
    if not wid:
        return None
    worker = (
        await db.execute(
            select(WorkerNode).where(
                WorkerNode.id == int(wid), WorkerNode.is_active.is_(True)
            )
        )
    ).scalar_one_or_none()
    if worker is None:
        return None
    cached, _ = wc.worker_health_cached(int(worker.id))
    if cached is False:
        return None
    token = wc.peek_raw_token(int(worker.id))
    if not token:
        # Token only in memory after create — workers use hash auth inbound;
        # outbound needs the raw token stored in meta after install handshake.
        token = str((worker.meta or {}).get("agent_token") or "") or None
    if not token:
        return None
    return worker, token


async def store_agent_token(db: AsyncSession, worker_id: int, token: str) -> None:
    """Persist raw token for master→worker calls (encrypted-at-rest via DB ACL)."""
    worker = await get_worker(db, worker_id)
    if worker is None:
        return
    meta = dict(worker.meta or {})
    meta["agent_token"] = token
    worker.meta = meta
    worker.token_hash = wc.hash_worker_token(token)
    await db.commit()
    wc.remember_raw_token(int(worker_id), token)


async def bootstrap_worker_routes(db: AsyncSession) -> int:
    """Load all workers into the sync route table (call on startup)."""
    rows = (await db.execute(select(WorkerNode))).scalars().all()
    n = 0
    for w in rows:
        tok = str((w.meta or {}).get("agent_token") or "") or wc.peek_raw_token(
            int(w.id)
        )
        if not tok:
            continue
        wc.register_worker_route(
            int(w.id),
            str(w.base_url),
            tok,
            active=bool(w.is_active),
        )
        n += 1
    return n


async def probe_all_workers(db: AsyncSession | None = None) -> dict[str, int]:
    """Ping workers without holding a DB session across HTTP awaits.

    Snapshot rows → close → ping → fresh session for status writes. Holding
    ``db`` across ``ping_worker`` let other bg tasks collide on the same
    asyncpg connection (InternalClientError).
    """
    from app.core.database import AsyncSessionLocal

    # Optional legacy arg ignored when provided — always use isolated sessions.
    _ = db
    async with AsyncSessionLocal() as read_db:
        rows = (await read_db.execute(select(WorkerNode))).scalars().all()
        snap = [
            {
                "id": int(w.id),
                "base_url": str(w.base_url),
                "is_active": bool(w.is_active),
                "token": wc.peek_raw_token(int(w.id))
                or str((w.meta or {}).get("agent_token") or ""),
            }
            for w in rows
        ]
        await read_db.rollback()

    results: list[tuple[int, str, str | None]] = []
    online = offline = 0
    for w in snap:
        token = w["token"]
        if token:
            wc.register_worker_route(
                int(w["id"]),
                str(w["base_url"]),
                token,
                active=bool(w["is_active"]),
            )
        if not w["is_active"]:
            continue
        if not token:
            results.append((int(w["id"]), "unknown", "missing token"))
            offline += 1
            continue
        ok, err = await wc.ping_worker(
            base_url=str(w["base_url"]), token=token
        )
        wc.mark_worker_health(int(w["id"]), ok, err)
        if ok:
            results.append((int(w["id"]), "online", None))
            online += 1
        else:
            results.append((int(w["id"]), "offline", (err or "")[:400]))
            offline += 1

    if results:
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as write_db:
            for wid, status, err in results:
                worker = await write_db.get(WorkerNode, int(wid))
                if worker is None:
                    continue
                worker.status = status
                if status == "online":
                    worker.last_seen_at = now
                    worker.last_error = None
                elif err:
                    worker.last_error = err
            await write_db.commit()
    return {"online": online, "offline": offline, "total": len(snap)}


async def sync_assignments_to_worker(
    db: AsyncSession, worker_id: int
) -> tuple[bool, str]:
    worker = await get_worker(db, worker_id)
    if worker is None:
        return False, "not found"
    token = wc.peek_raw_token(int(worker.id)) or str(
        (worker.meta or {}).get("agent_token") or ""
    )
    if not token:
        return False, "توکن در حافظه نیست — یک‌بار Rotate یا نصب مجدد"
    from app.models.panel import PanelType

    panels = []
    for p in worker.panels or []:
        if not p.is_active:
            continue
        # Astra nodes need master DB/xray — never assign to remote worker agent.
        if getattr(p, "type", None) == PanelType.astra:
            continue
        panels.append(wc.panel_snapshot(p))
    master = _master_public_url()
    if not master:
        return False, "PANEL_BASE_URL تنظیم نشده"
    # Keep sync route table warm so get_adapter (create/delete/usage) hits Worker.
    wc.register_worker_route(
        int(worker.id),
        str(worker.base_url),
        token,
        active=bool(worker.is_active),
    )
    ok, err = await wc.push_assignments(
        base_url=worker.base_url,
        token=token,
        master_url=master,
        panels=panels,
        # Warm-tier staggered waves (cold full sweep every ~120s on agent).
        interval_sec=25,
    )
    return ok, err


async def push_agent_update_to_worker(
    db: AsyncSession, worker_id: int
) -> tuple[bool, str]:
    """Ask a live worker to pull the current master worker-bundle and restart."""
    import httpx

    from app.services.version_info import get_version

    worker = await get_worker(db, worker_id)
    if worker is None:
        return False, "not found"
    if not worker.is_active:
        return False, "inactive"
    token = wc.peek_raw_token(int(worker.id)) or str(
        (worker.meta or {}).get("agent_token") or ""
    )
    if not token:
        return False, "no token"
    master = _master_public_url()
    if not master:
        return False, "PANEL_BASE_URL missing"
    url = normalize_panel_base_url(worker.base_url).rstrip("/") + "/v1/self-update"
    body = {
        "bundle_url": master.rstrip("/") + "/api/public/worker-bundle.tgz",
        "expected_version": get_version(),
    }
    try:
        async with httpx.AsyncClient(timeout=90.0, verify=False) as client:
            res = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        if res.status_code != 200:
            return False, f"HTTP {res.status_code}: {(res.text or '')[:180]}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:240]
    # Re-push panel assignments after agent restart settles.
    await asyncio.sleep(4.0)
    ok, err = await sync_assignments_to_worker(db, int(worker.id))
    if not ok:
        return True, f"updated; assign sync: {err}"
    return True, "updated"


async def push_agent_update_to_all_workers(db: AsyncSession) -> dict[str, Any]:
    """Hot-update every active worker after master apply-update."""
    rows = (await db.execute(select(WorkerNode))).scalars().all()
    ok_n = fail_n = skip_n = 0
    details: list[dict[str, Any]] = []
    for w in rows:
        if not w.is_active:
            skip_n += 1
            continue
        ok, msg = await push_agent_update_to_worker(db, int(w.id))
        details.append({"id": int(w.id), "name": w.name, "ok": ok, "msg": msg})
        if ok:
            ok_n += 1
        else:
            fail_n += 1
            logger.warning(
                "worker self-update failed id=%s name=%s err=%s",
                w.id,
                w.name,
                msg[:160],
            )
    logger.info(
        "worker self-update done ok=%s fail=%s skip=%s", ok_n, fail_n, skip_n
    )
    return {"ok": ok_n, "fail": fail_n, "skip": skip_n, "details": details}
