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
# login/API always keep headroom in the pool.
DB_BACKGROUND_SLOTS = 4
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

    # Postgres / other servers — sized for shared hosts, fail-fast on starve.
    pool_size = max(2, int(getattr(settings, "DB_POOL_SIZE", 8) or 8))
    max_overflow = max(0, int(getattr(settings, "DB_MAX_OVERFLOW", 12) or 12))
    pool_timeout = float(getattr(settings, "DB_POOL_TIMEOUT", 3.0) or 3.0)
    pool_recycle = max(60, int(getattr(settings, "DB_POOL_RECYCLE", 300) or 300))
    kwargs.update(
        {
            "pool_size": pool_size,
            "max_overflow": max_overflow,
            "pool_timeout": pool_timeout,
            "pool_recycle": pool_recycle,
            "pool_use_lifo": True,
            "pool_reset_on_return": "rollback",
        }
    )
    # asyncpg
    if "+asyncpg" in _URL or _URL.startswith("postgresql+asyncpg"):
        kwargs["connect_args"] = {
            "timeout": 10,
            "command_timeout": 60,
            "server_settings": {
                "statement_timeout": "60000",
                "idle_in_transaction_session_timeout": "30000",
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


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Request-scoped session — always closed, even on CancelledError."""
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


async def pool_guardian_loop(interval: float = 45.0) -> None:
    """Watch pool pressure; recycle when stuck near capacity."""
    await asyncio.sleep(20)
    high_streak = 0
    while True:
        try:
            st = pool_status()
            pressure = st.get("pressure") or "ok"
            if pressure == "critical":
                high_streak += 1
                logger.warning("db pool critical: %s", st)
                if high_streak >= 3:
                    # Soft recovery: dispose idle pooled conns; in-flight keep working
                    logger.error(
                        "db pool stuck at capacity — disposing engine to recover"
                    )
                    await dispose_engine()
                    high_streak = 0
            elif pressure == "high":
                high_streak = min(high_streak + 1, 2)
                logger.info("db pool high pressure: %s", st)
            else:
                high_streak = 0
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("pool guardian failed")
        await asyncio.sleep(interval)
