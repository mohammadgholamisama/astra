"""Async SQLAlchemy engine + session helpers — pool must never starve the panel."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger("astra.db")

_URL = (settings.DATABASE_URL or "").strip()
_IS_SQLITE = _URL.startswith("sqlite")

# Background jobs (quota / heal / outbound ping) share this cap so interactive
# login/API always keep headroom in the pool. One slot only — concurrent bg
# sessions were exhausting QueuePool and stalling the whole panel UI.
DB_BACKGROUND_SLOTS = 1
_bg_sem: asyncio.Semaphore | None = None


def background_db_sem() -> asyncio.Semaphore:
    """Lazy semaphore — must be created inside a running event loop."""
    global _bg_sem
    if _bg_sem is None:
        _bg_sem = asyncio.Semaphore(DB_BACKGROUND_SLOTS)
    return _bg_sem


def _build_engine_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "echo": False,
        # Prefer hottest connections; cuts stampede on idle/dead sockets.
        "pool_pre_ping": True,
    }
    if _IS_SQLITE:
        # NullPool: checkout = open, checkin = close. No QueuePool exhaustion
        # on SQLite (WAL + busy_timeout handle concurrency).
        kwargs.update(
            {
                "poolclass": NullPool,
                "connect_args": {"check_same_thread": False, "timeout": 60},
            }
        )
        return kwargs

    # Postgres — small pool so one uvicorn cannot open 70+ backends and
    # starve SSH/CPU. Fail-fast on starve; bg work uses background_db_sem.
    pool_size = max(2, int(getattr(settings, "DB_POOL_SIZE", 16) or 16))
    max_overflow = max(0, int(getattr(settings, "DB_MAX_OVERFLOW", 8) or 8))
    pool_timeout = float(getattr(settings, "DB_POOL_TIMEOUT", 5.0) or 5.0)
    # Recycle before server/proxy idle kills the socket (default 300s).
    pool_recycle = max(60, int(getattr(settings, "DB_POOL_RECYCLE", 300) or 300))
    kwargs.update(
        {
            "pool_size": pool_size,
            "max_overflow": max_overflow,
            "pool_timeout": pool_timeout,
            "pool_recycle": pool_recycle,
            "pool_pre_ping": True,  # explicit — drop dead sockets before checkout
            "pool_use_lifo": True,
            "pool_reset_on_return": "rollback",
        }
    )
    # asyncpg
    if "+asyncpg" in _URL or _URL.startswith("postgresql+asyncpg"):
        kwargs["connect_args"] = {
            "timeout": 8,
            "command_timeout": 45,
            "server_settings": {
                "statement_timeout": "45000",
                # Safety net for leaked idle-in-transaction checkouts. Keep
                # well above normal recount/sample txn length — an 8s kill
                # raced concurrent bg work and poisoned asyncpg checkouts
                # (BrokenPipe / InternalClientError).
                "idle_in_transaction_session_timeout": "60000",
            },
        }
    return kwargs


engine = create_async_engine(_URL, **_build_engine_kwargs())

if _IS_SQLITE:

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_on_connect(dbapi_conn, _connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA mmap_size=268435456")
        cursor.close()


AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    pass


def pool_status() -> dict[str, Any]:
    """Snapshot of pool health (safe for /api/system)."""
    pool = getattr(engine.sync_engine, "pool", None)
    if pool is None:
        return {"engine": "unknown", "sqlite": _IS_SQLITE}
    info: dict[str, Any] = {
        "engine": type(pool).__name__,
        "sqlite": _IS_SQLITE,
    }
    for attr in ("size", "checkedin", "checkedout", "overflow", "invalidatedcount"):
        fn = getattr(pool, attr, None)
        if callable(fn):
            try:
                info[attr] = int(fn())
            except Exception:  # noqa: BLE001
                pass
    try:
        # QueuePool only
        info["pool_size"] = int(getattr(pool, "size")())
        info["max_overflow"] = int(getattr(pool, "_max_overflow", -1))
    except Exception:  # noqa: BLE001
        pass
    checked = int(info.get("checkedout") or 0)
    overflow = int(info.get("overflow") or 0)
    size = int(info.get("size") or info.get("pool_size") or 0)
    cap = size + max(0, int(info.get("max_overflow") or 0))
    info["in_use"] = checked
    info["capacity"] = cap if cap > 0 else None
    if cap > 0:
        info["utilization"] = round(checked / cap, 3)
        info["pressure"] = "critical" if checked >= cap else (
            "high" if checked >= max(1, int(cap * 0.75)) else "ok"
        )
    else:
        info["pressure"] = "ok"
    return info


async def release_db_txn(session: AsyncSession) -> None:
    """End the open transaction so Postgres is not left ``idle in transaction``.

    Call this before any slow non-DB await (remote HTTP, sleep, subprocess)
    while still holding a request-scoped session. Read-only → rollback;
    dirty → commit.

    Note: rollback *expires* ORM instances (even with ``expire_on_commit=False``).
    Snapshot needed fields or ``expunge`` before calling this if you will use
    objects after the release.
    """
    try:
        in_txn = bool(session.in_transaction())
    except Exception:  # noqa: BLE001
        in_txn = True
    if not in_txn:
        return
    dirty = bool(session.new or session.dirty or session.deleted)
    try:
        if dirty:
            await session.commit()
        else:
            await session.rollback()
    except Exception:  # noqa: BLE001
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Request-scoped session — always closed, even on CancelledError.

    Does not auto-commit: handlers that mutate must ``await db.commit()``.
    Always ends any open txn on exit so Postgres never keeps
    ``idle in transaction`` after the response.
    """
    session = AsyncSessionLocal()
    try:
        yield session
    except BaseException:
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        try:
            if session.is_active and session.in_transaction():
                await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        try:
            await session.close()
        except Exception:  # noqa: BLE001
            pass


@asynccontextmanager
async def session_scope(*, background: bool = False) -> AsyncIterator[AsyncSession]:
    """Short-lived session for services / background work.

    ``background=True`` acquires the global BG semaphore so loops cannot
    starve login/API connections.
    """
    sem = background_db_sem() if background else None
    if sem is not None:
        await sem.acquire()
    session = AsyncSessionLocal()
    try:
        yield session
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    except BaseException:
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        try:
            await session.close()
        except Exception:  # noqa: BLE001
            pass
        if sem is not None:
            sem.release()


async def dispose_engine() -> None:
    """Drop pooled connections (shutdown / recovery)."""
    try:
        await engine.dispose()
        logger.info("db engine disposed")
    except Exception:  # noqa: BLE001
        logger.exception("engine dispose failed")


async def ping_db() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        logger.warning("db ping failed", exc_info=True)
        return False


def pool_pressure_blocks_background() -> bool:
    """True when interactive API should get exclusive pool headroom."""
    st = pool_status()
    pressure = st.get("pressure") or "ok"
    if pressure == "critical":
        return True
    if pressure == "high":
        return True
    checked = int(st.get("checkedout") or st.get("in_use") or 0)
    cap = int(st.get("capacity") or 0)
    return bool(cap > 0 and checked >= max(1, cap - 2))


def pool_pressure_blocks_fleet() -> bool:
    """Only block fleet bulk on critical pressure.

    Fleet HTTP runs outside the pool; short apply/recount sessions are cheap.
    Treating ``high`` like critical starved meters for hours (used_traffic=0).
    """
    st = pool_status()
    if (st.get("pressure") or "ok") == "critical":
        return True
    checked = int(st.get("checkedout") or st.get("in_use") or 0)
    cap = int(st.get("capacity") or 0)
    # Only yield when nearly every connection is checked out.
    return bool(cap > 0 and checked >= max(2, cap - 1))


async def _terminate_stale_idle_in_txn(*, older_than_sec: float = 90.0) -> int:
    """Best-effort: kill Postgres backends stuck idle-in-transaction."""
    if _IS_SQLITE:
        return 0
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT count(pg_terminate_backend(pid)) "
                    "FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() "
                    "AND state = 'idle in transaction' "
                    "AND xact_start < now() - (:sec * interval '1 second')"
                ),
                {"sec": float(older_than_sec)},
            )
            await conn.commit()
            return int(result.scalar() or 0)
    except Exception:  # noqa: BLE001
        logger.debug("idle-in-txn terminate skipped", exc_info=True)
        return 0


async def pool_guardian_loop(interval: float = 20.0) -> None:
    """Self-heal pool stalls: reap only long-stuck idle-in-txn; never dispose."""
    await asyncio.sleep(30)
    high_streak = 0
    while True:
        try:
            # Only reap truly stuck backends (>90s). Aggressive 8s kills were
            # racing live recount/sample sessions mid-statement.
            killed = await _terminate_stale_idle_in_txn(older_than_sec=90.0)
            if killed:
                logger.warning(
                    "pool guardian reaped %s idle-in-transaction backends", killed
                )
            st = pool_status()
            pressure = st.get("pressure") or "ok"
            if pressure in ("critical", "high"):
                high_streak += 1
                logger.warning("db pool %s: %s", pressure, st)
                if pressure == "critical":
                    await _terminate_stale_idle_in_txn(older_than_sec=60.0)
                # Do NOT dispose/recycle the pool under pressure — that races
                # checked-out asyncpg connections and causes
                # InternalClientError: another operation is in progress.
                if pressure == "critical" or high_streak >= 2:
                    logger.error("db pool pressure (no dispose): %s", st)
                    high_streak = 0
            else:
                high_streak = 0
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("pool guardian failed")
        await asyncio.sleep(interval)
