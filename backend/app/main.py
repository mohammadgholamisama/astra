import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api import (
    analytics,
    api_keys,
    astra_outbounds,
    auth,
    backup,
    config_locations,
    config_lookup,
    config_orders,
    group_configs,
    groups,
    panels,
    settings as settings_api,
    subscription,
    system,
    templates,
    users,
    workers as workers_api,
)
# ensure new models are registered for schema sync
from app.models import astra_outbound as _astra_outbound_model  # noqa: F401
from app.models import config_link_order as _config_link_order_model  # noqa: F401
from app.models import config_location as _config_location_model  # noqa: F401
from app.models import group_config as _group_config_model  # noqa: F401
from app.models import group_config_hide as _group_config_hide_model  # noqa: F401
from app.models import site_setting as _site_setting_model  # noqa: F401
from app.models import user_analytics as _user_analytics_model  # noqa: F401
from app.models import panel_analytics as _panel_analytics_model  # noqa: F401
from app.models import worker as _worker_model  # noqa: F401
from app.models.user import PanelAccount, User, UserStatus
from app.core.config import get_settings
from app.core.database import (
    AsyncSessionLocal,
    dispose_engine,
    pool_guardian_loop,
    session_scope,
)
from app.core.schema_sync import ensure_schema
from app.seed import seed_admin
from app.services.config_hides import refresh_cache as refresh_config_hides
from app.services.config_locations import refresh_cache as refresh_config_locations
from app.services.config_orders import refresh_cache as refresh_config_orders
from app.services.site_settings import refresh_cache as refresh_site_settings

settings = get_settings()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("astra")


async def _auto_retry_errors_loop() -> None:
    """Optional auto «رفع خطا» — off by default (Users button is enough).

    When enabled via AUTO_RETRY_ERRORS=1, chews a small fair batch so /sub
    stays responsive. Large sticky piles used to burn CPU/DB and freeze SSH.
    """
    from sqlalchemy import and_, func, or_, select

    from app.core.database import AsyncSessionLocal, background_db_sem
    from app.models.panel import Panel
    from app.models.user import PanelAccount, PanelAccountStatus
    from app.services.orchestrator import (
        heal_users_missing_panel_accounts,
        retry_all_failed_accounts,
        sweep_stale_panel_errors,
    )

    if not bool(getattr(settings, "AUTO_RETRY_ERRORS", False)):
        logger.info(
            "auto retry-errors disabled — use Users «رفع خطا» manually"
        )
        return

    await asyncio.sleep(45)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                sweep = await sweep_stale_panel_errors(db)
                if sweep.get("swept_active") or sweep.get("swept_pending"):
                    logger.info(
                        "auto retry-errors sweep active=%s pending=%s",
                        sweep.get("swept_active"),
                        sweep.get("swept_pending"),
                    )
                n_err = int(
                    (
                        await db.scalar(
                            select(func.count(PanelAccount.id))
                            .select_from(PanelAccount)
                            .join(Panel, Panel.id == PanelAccount.panel_id)
                            .where(
                                PanelAccount.status.in_(
                                    (
                                        PanelAccountStatus.error,
                                        PanelAccountStatus.pending,
                                    )
                                ),
                                Panel.is_active.is_(True),
                                or_(
                                    PanelAccount.status == PanelAccountStatus.pending,
                                    PanelAccount.error_message.is_(None),
                                    PanelAccount.error_message == "",
                                    and_(
                                        ~PanelAccount.error_message.ilike(
                                            "%user limit reached%"
                                        ),
                                        ~PanelAccount.error_message.ilike(
                                            "%limit reached%"
                                        ),
                                        ~PanelAccount.error_message.ilike("%ظرفیت%"),
                                    ),
                                ),
                            )
                        )
                    )
                    or 0
                )
            if n_err > 0:
                if n_err > 5000:
                    limit, conc, overall = 40, 3, 60.0
                elif n_err > 800:
                    limit, conc, overall = 60, 4, 75.0
                else:
                    limit, conc, overall = 80, 5, 90.0
                logger.info(
                    "auto retry-errors: backlog=%s batch=%s",
                    n_err,
                    limit,
                )
                sem = background_db_sem()
                await sem.acquire()
                try:
                    async with AsyncSessionLocal() as db:
                        stats = await retry_all_failed_accounts(
                            db,
                            overall_timeout=overall,
                            per_remote_timeout=12.0,
                            probe_timeout=5.0,
                            concurrency=conc,
                            limit_accounts=limit,
                        )
                        try:
                            await db.commit()
                        except Exception:  # noqa: BLE001
                            pass
                        logger.info(
                            "auto retry-errors done attempted=%s recovered=%s "
                            "remaining=%s swept_active=%s swept_pending=%s",
                            stats.get("attempted"),
                            stats.get("recovered"),
                            stats.get("remaining"),
                            stats.get("swept_active"),
                            stats.get("swept_pending"),
                        )
                    try:
                        await heal_users_missing_panel_accounts(limit_users=10)
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            "auto retry-errors heal-coverage skipped",
                            exc_info=True,
                        )
                finally:
                    sem.release()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("auto retry-errors loop failed")
        try:
            async with AsyncSessionLocal() as db:
                left = int(
                    (
                        await db.scalar(
                            select(func.count(PanelAccount.id)).where(
                                PanelAccount.status.in_(
                                    (
                                        PanelAccountStatus.error,
                                        PanelAccountStatus.pending,
                                    )
                                )
                            )
                        )
                    )
                    or 0
                )
        except Exception:  # noqa: BLE001
            left = 0
        await asyncio.sleep(180 if left > 0 else 600)



async def _astra_xray_heal_loop() -> None:
    """Keep astra-xray@N up — calm cadence; never stampede systemctl/D-Bus."""
    from app.services import astra_node as node

    await asyncio.sleep(90)
    while True:
        try:
            stats = await node.ensure_all_astra_xray_running(limit=40)
            if stats.get("started") or stats.get("failed"):
                logging.getLogger("astra").info(
                    "xray heal: started=%s skipped=%s missing=%s failed=%s",
                    stats.get("started"),
                    stats.get("skipped"),
                    stats.get("missing"),
                    stats.get("failed"),
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logging.getLogger("astra").exception("xray heal loop failed")
        await asyncio.sleep(600)


async def _outbound_health_loop() -> None:
    """Re-ping Astra outbounds so recovered ones reappear in subscription.

    When an outbound fails ping it is dropped from share-links; without this
    loop it stayed missing until an admin clicked Ping even after recovery.
    Uses background DB slots so this never starves login/API.
    """
    from app.models.panel import Panel, PanelType
    from app.services import astra_node as node

    await asyncio.sleep(90)
    while True:
        try:
            async with session_scope(background=True) as db:
                result = await db.execute(
                    select(Panel.id).where(
                        Panel.type == PanelType.astra,
                        Panel.is_active.is_(True),
                    )
                )
                panel_ids = [int(r[0]) for r in result.all()]
            for pid in panel_ids:
                try:
                    stats = await node.reconcile_outbounds_health_released(
                        pid, concurrency=2
                    )
                    if stats.get("changed"):
                        logger.info(
                            "outbound health: panel=%s pinged=%s changed=%s ok=%s",
                            pid,
                            stats.get("pinged"),
                            stats.get("changed"),
                            stats.get("ok"),
                        )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "outbound health reconcile failed panel=%s", pid
                    )
                await asyncio.sleep(1.5)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("outbound health loop failed")
        await asyncio.sleep(300)


async def _usage_sync_loop() -> None:
    """Cadenced quota watch: sync Astra traffic, freeze exhausted remotes.

    Important: if a cycle is heavy (thousands of accounts), do NOT immediately
    re-enter — that was pegging CPU at ~100%. Always cool down at least
    ``USAGE_REFRESH_SECONDS``, and longer after slow cycles.
    """
    import time as _time

    from app.core.database import background_db_sem
    from app.services.orchestrator import run_quota_watch_cycle

    while True:
        t0 = _time.monotonic()
        elapsed = 0.0
        try:
            from app.core.database import (
                pool_pressure_blocks_background,
                pool_pressure_blocks_fleet,
            )

            # Under high pool pressure still do DB-only recount+flip so
            # used_traffic catches meters and over-limit users get limited.
            if pool_pressure_blocks_background():
                if pool_pressure_blocks_fleet():
                    logger.warning(
                        "quota watch deferred — db pool critical"
                    )
                    await asyncio.sleep(30)
                    continue
                try:
                    from app.services import panel_usage_bulk as bulk_usage

                    light = await bulk_usage.recount_and_flip_exhausted(
                        limit=400, concurrency=4
                    )
                    exhausted = await bulk_usage.find_exhausted_user_ids(
                        limit=80
                    )
                    freeze_ok = freeze_fail = 0
                    if exhausted:
                        from app.services.orchestrator import (
                            _freeze_quota_users,
                        )

                        freeze_ok, freeze_fail = await _freeze_quota_users(
                            exhausted[:60],
                            remote_timeout=6.0,
                            concurrency=8,
                            wall=18.0,
                        )
                    logger.warning(
                        "quota watch light (pool high): lag=%s raw=%s "
                        "flipped=%s exhausted=%s freeze_ok=%s freeze_fail=%s",
                        light.get("lagging"),
                        light.get("raw_over"),
                        light.get("flipped"),
                        light.get("exhausted"),
                        freeze_ok,
                        freeze_fail,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("quota watch light cycle failed")
                await asyncio.sleep(35)
                continue
            sem = background_db_sem()
            await sem.acquire()
            try:
                stats = await run_quota_watch_cycle()
            finally:
                sem.release()
            elapsed = _time.monotonic() - t0
            if (
                stats.get("flipped")
                or stats.get("freeze_ok")
                or stats.get("freeze_fail")
                or elapsed >= 8
            ):
                logger.info(
                    "quota watch: astra=%s checked=%s flipped=%s freeze_ok=%s freeze_fail=%s pulled=%s dt=%.2fs",
                    stats.get("astra_panels"),
                    stats.get("checked"),
                    stats.get("flipped"),
                    stats.get("freeze_ok"),
                    stats.get("freeze_fail"),
                    stats.get("usage_pulled"),
                    elapsed,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("quota watch loop failed")
            elapsed = _time.monotonic() - t0
        interval = max(90, int(settings.USAGE_REFRESH_SECONDS or 120))
        # Heavy cycle → cool down so we don't re-enter while remotes still drain CPU
        if elapsed > interval:
            cooldown = min(120.0, max(float(interval), elapsed * 0.4))
        else:
            cooldown = max(60.0, float(interval) - elapsed)
        try:
            from app.services.worker_client import any_worker_healthy
            from app.services.worker_ops import usage_report_queue_stats

            if any_worker_healthy():
                cooldown = max(cooldown, 100.0)
            if int(usage_report_queue_stats().get("qsize") or 0) >= 6:
                cooldown = max(cooldown, 140.0)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(cooldown)


async def _fleet_usage_loop() -> None:
    """Cadenced fleet/rotate bulk — must NOT hold background_db_sem for HTTP.

    Holding the single BG slot for a full fleet pass starved meters for hours
    (panel_accounts last_sync stale → users.used_traffic stuck at 0).
    """
    import time as _time

    from app.services.panel_usage_bulk import run_fleet_usage_cycle

    # Let boot upserts settle so fleet does not compete for every pool slot.
    await asyncio.sleep(35)
    while True:
        t0 = _time.monotonic()
        try:
            from app.core.database import pool_pressure_blocks_fleet

            if pool_pressure_blocks_fleet():
                logger.warning("fleet usage deferred — db pool critical")
                await asyncio.sleep(25)
                continue
            # No background_db_sem around HTTP — cycle uses short sessions only.
            stats = await run_fleet_usage_cycle(rotate_only=True)
            logger.info(
                "fleet usage: astra=%s panels=%s listed=%s dirty=%s "
                "lagging=%s recount=%s flipped=%s dt=%ss",
                stats.get("astra_panels"),
                stats.get("panels"),
                stats.get("listed"),
                stats.get("dirty"),
                stats.get("lagging"),
                stats.get("recount_users"),
                stats.get("flipped"),
                stats.get("dt"),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("fleet usage loop failed")
        interval = max(90, int(getattr(settings, "USAGE_FLEET_SECONDS", 120) or 120))
        # When a worker is healthy, stretch fleet — it owns remote bulk meters.
        try:
            from app.services.worker_client import any_worker_healthy
            from app.services.worker_ops import usage_report_queue_stats

            if any_worker_healthy():
                interval = max(interval, 150)
            if int(usage_report_queue_stats().get("qsize") or 0) >= 8:
                interval = max(interval, 180)
        except Exception:  # noqa: BLE001
            pass
        elapsed = _time.monotonic() - t0
        await asyncio.sleep(max(60.0, float(interval) - elapsed))


async def _hot_usage_loop() -> None:
    """Cadenced live-pull for recent /sub users — short sessions, no BG-sem hold."""
    import time as _time

    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.services.orchestrator import (
        recount_user_usage_pool_safe,
        refresh_usage_pool_safe,
        take_live_usage_priority,
    )

    await asyncio.sleep(6)
    while True:
        t0 = _time.monotonic()
        ids: list[int] = []
        try:
            from app.core.database import pool_pressure_blocks_fleet

            # Never fully skip on pressure — priority consumers must keep updating.
            under_pressure = False
            try:
                under_pressure = bool(pool_pressure_blocks_fleet())
            except Exception:  # noqa: BLE001
                under_pressure = False
            if under_pressure:
                logger.warning(
                    "hot usage pool pressure — priority-only lane"
                )
            ids = list(take_live_usage_priority(300.0, 24))
            if not under_pressure:
                try:
                    from app.models.user_analytics import UserUsageState
                    from app.models.user import User, UserStatus
                    from app.services.panel_usage_bulk import find_stale_sync_user_ids
                    from sqlalchemy import or_

                    since = datetime.now(timezone.utc) - timedelta(minutes=20)
                    now_ts = int(_time.time())
                    # Stale meters first — consuming users must not wait for fleet.
                    try:
                        stale = await find_stale_sync_user_ids(
                            older_than_sec=60, limit=12
                        )
                        ids = list(dict.fromkeys(stale + ids))
                    except Exception:  # noqa: BLE001
                        pass
                    async with AsyncSessionLocal() as db:
                        recent = [
                            int(uid)
                            for (uid,) in (
                                await db.execute(
                                    select(UserUsageState.user_id)
                                    .where(
                                        UserUsageState.last_sub_fetch_at.is_not(
                                            None
                                        )
                                    )
                                    .where(
                                        UserUsageState.last_sub_fetch_at >= since
                                    )
                                    .order_by(
                                        UserUsageState.last_sub_fetch_at.desc()
                                    )
                                    .limit(16)
                                )
                            ).all()
                        ]
                        ids = list(dict.fromkeys(ids + recent))[:32]
                        if ids:
                            live = (
                                await db.execute(
                                    select(User.id).where(
                                        User.id.in_(ids),
                                        User.status.in_(
                                            (
                                                UserStatus.active,
                                                UserStatus.on_hold,
                                                UserStatus.limited,
                                            )
                                        ),
                                        or_(
                                            User.expire == 0,
                                            User.expire.is_(None),
                                            User.expire > now_ts,
                                        ),
                                    )
                                )
                            ).all()
                            ids = [int(r[0]) for r in live]
                        await db.rollback()
                except Exception:  # noqa: BLE001
                    pass

            ids = ids[:3] if under_pressure else ids[:10]
            if ids:
                sem = asyncio.Semaphore(1 if under_pressure else 2)

                async def _one(uid: int) -> None:
                    async with sem:
                        try:
                            await recount_user_usage_pool_safe(int(uid))
                            await asyncio.wait_for(
                                refresh_usage_pool_safe(
                                    int(uid), force=True, quick=True
                                ),
                                timeout=28.0,
                            )
                        except asyncio.TimeoutError:
                            try:
                                await recount_user_usage_pool_safe(int(uid))
                            except Exception:  # noqa: BLE001
                                logger.warning(
                                    "hot usage timeout recount user_id=%s",
                                    uid,
                                    exc_info=True,
                                )
                        except Exception:  # noqa: BLE001
                            logger.warning(
                                "hot usage pull user_id=%s failed",
                                uid,
                                exc_info=True,
                            )

                await asyncio.gather(
                    *[_one(uid) for uid in ids], return_exceptions=True
                )
                logger.info(
                    "hot usage: pulled=%s pressure=%s dt=%.1fs",
                    len(ids),
                    int(under_pressure),
                    _time.monotonic() - t0,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("hot usage loop failed")
        interval = max(30, int(getattr(settings, "USAGE_HOT_SECONDS", 45) or 45))
        elapsed = _time.monotonic() - t0
        await asyncio.sleep(max(25.0, float(interval) - elapsed))


async def _priority_usage_loop() -> None:
    """Dedicated lane for live consumers — never deferred by fleet/pool pressure.

    After check-usage / successful pull, users stay in the priority set ~5 min.
    This loop alone must keep their meters within ~30–45s of remote panels.
    """
    import time as _time

    from app.services.orchestrator import (
        recount_user_usage_pool_safe,
        refresh_usage_pool_safe,
        take_live_usage_priority,
    )

    await asyncio.sleep(10)
    while True:
        t0 = _time.monotonic()
        try:
            ids = list(take_live_usage_priority(360.0, 6))[:4]
            if ids:
                for uid in ids:
                    try:
                        await asyncio.wait_for(
                            refresh_usage_pool_safe(
                                int(uid), force=True, quick=True
                            ),
                            timeout=25.0,
                        )
                    except asyncio.TimeoutError:
                        try:
                            await recount_user_usage_pool_safe(int(uid))
                        except Exception:  # noqa: BLE001
                            pass
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "priority usage pull user_id=%s failed",
                            uid,
                            exc_info=True,
                        )
                logger.info(
                    "priority usage: pulled=%s dt=%.1fs",
                    len(ids),
                    _time.monotonic() - t0,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("priority usage loop failed")
        await asyncio.sleep(max(20.0, 30.0 - (_time.monotonic() - t0)))


async def _worker_probe_loop() -> None:
    """Keep worker online/offline status fresh for failover routing."""
    from app.core.database import AsyncSessionLocal
    from app.services.worker_ops import probe_all_workers

    await asyncio.sleep(20)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                stats = await probe_all_workers(db)
            logger.info(
                "worker probe: online=%s offline=%s total=%s",
                stats.get("online"),
                stats.get("offline"),
                stats.get("total"),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("worker probe loop failed")
        await asyncio.sleep(30)


async def _analytics_sample_loop() -> None:
    """Cadenced analytics snapshot; live Astra sync stays light."""
    from app.core.database import AsyncSessionLocal, background_db_sem
    from app.services import panel_analytics as panel_analytics_svc
    from app.services import user_analytics as analytics_svc

    await asyncio.sleep(20)
    cycle = 0
    while True:
        try:
            sem = background_db_sem()
            await sem.acquire()
            try:
                stats = await analytics_svc.sample_all_users_once()
                panel_stats = await panel_analytics_svc.sample_all_panels_once()
                # Every ~3 minutes: live Xray sync so hour columns track consumption
                if cycle % 3 == 0:
                    live = await panel_analytics_svc.refresh_panel_analytics_fast(
                        tz_offset_min=0,
                        budget_s=5.5,
                    )
                    live_stats = (live or {}).get("stats") or {}
                    logger.info(
                        "analytics live refresh: astra=%s bytes=+%s ms=%s err=%s",
                        live_stats.get("astra_synced"),
                        live_stats.get("bytes_added"),
                        live_stats.get("ms"),
                        live_stats.get("error"),
                    )
            finally:
                sem.release()
            cycle += 1
            if cycle == 1 or cycle % 30 == 0:
                logger.info("analytics sample: users=%s panels=%s", stats, panel_stats)
            if cycle % 120 == 0:
                async with AsyncSessionLocal() as db:
                    pruned = await analytics_svc.prune_analytics(db)
                    ppruned = await panel_analytics_svc.prune_panel_analytics(db)
                    if any(pruned.values()) or any(ppruned.values()):
                        logger.info(
                            "analytics prune: users=%s panels=%s", pruned, ppruned
                        )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("analytics sample loop failed")
        await asyncio.sleep(300)


def _resolve_frontend_dist() -> Path | None:
    """Locate built SPA (production install or local frontend/dist)."""
    candidates: list[Path] = []
    custom = (settings.FRONTEND_DIST or "").strip()
    if custom:
        candidates.append(Path(custom))
    # /opt/astra layout
    candidates.append(Path("/opt/astra/frontend/dist"))
    # repo root relative to backend/app/main.py → parents[3] = repo
    root = Path(__file__).resolve().parents[3]
    candidates.append(root / "frontend" / "dist")
    # backend sibling
    candidates.append(Path(__file__).resolve().parents[2].parent / "frontend" / "dist")
    for p in candidates:
        try:
            if p.is_dir() and (p / "index.html").is_file():
                return p.resolve()
        except OSError:
            continue
    return None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Minimal boot first — /health must answer before any heavy bg work.
    from app.core.process_role import acquire_background_role

    run_bg = acquire_background_role()
    await ensure_schema()
    await seed_admin()
    async with AsyncSessionLocal() as db:
        await refresh_site_settings(db)
        await refresh_config_locations(db)
        await refresh_config_hides(db)
        await refresh_config_orders(db)
        try:
            from app.services.worker_ops import (
                bootstrap_worker_routes,
                ensure_usage_report_worker,
            )

            n = await bootstrap_worker_routes(db)
            if n:
                logging.getLogger("astra.worker").info(
                    "worker routes loaded n=%s", n
                )
            # Every web worker accepts usage-report and applies via local queue.
            await ensure_usage_report_worker()
        except Exception:  # noqa: BLE001
            logging.getLogger("astra.worker").warning(
                "worker route bootstrap failed", exc_info=True
            )
        await db.rollback()

    from app.services.config_hides import clear_stale_ping_auto_hides_once
    from app.services.config_orders import (
        relayout_order_slots_once,
        seed_order_kind_from_folders_once,
    )

    async def _bg_order_kind_boot() -> None:
        try:
            await asyncio.sleep(15)
            async with AsyncSessionLocal() as s:
                await clear_stale_ping_auto_hides_once(s)
                await seed_order_kind_from_folders_once(s)
                await relayout_order_slots_once(s)
        except Exception:  # noqa: BLE001
            logging.getLogger("astra").exception("order_kind boot task failed")

    # Stagger bg loops so boot does not stampede CPU/DB in the first minute.
    async def _delayed(coro, delay: float, name: str):
        await asyncio.sleep(delay)
        await coro()

    bg_tasks: list[asyncio.Task] = []
    if run_bg:
        # Longer stagger: Worker owns remote bulk; avoid boot stampede.
        bg_tasks = [
            asyncio.create_task(
                _delayed(_usage_sync_loop, 90, "usage"), name="astra-usage-sync"
            ),
            asyncio.create_task(
                _delayed(_fleet_usage_loop, 75, "fleet"), name="astra-fleet-usage"
            ),
            asyncio.create_task(
                _delayed(_hot_usage_loop, 40, "hot"), name="astra-hot-usage"
            ),
            asyncio.create_task(
                _delayed(_priority_usage_loop, 35, "priority"),
                name="astra-priority-usage",
            ),
            asyncio.create_task(
                _delayed(_worker_probe_loop, 20, "worker-probe"),
                name="astra-worker-probe",
            ),
            asyncio.create_task(
                _delayed(_analytics_sample_loop, 240, "analytics"),
                name="astra-analytics-sample",
            ),
            asyncio.create_task(
                _auto_retry_errors_loop(), name="astra-auto-retry-errors"
            ),
            asyncio.create_task(
                _delayed(_outbound_health_loop, 120, "outbound"),
                name="astra-outbound-health",
            ),
            asyncio.create_task(
                _delayed(_astra_xray_heal_loop, 240, "xray-heal"),
                name="astra-xray-heal",
            ),
            asyncio.create_task(
                _bg_order_kind_boot(), name="astra-order-kind-boot"
            ),
            asyncio.create_task(
                pool_guardian_loop(), name="astra-db-pool-guard"
            ),
        ]
        logger.info("astra lifespan: background loops ON (this worker)")
    else:
        logger.info("astra lifespan: API-only worker (bg loops elsewhere)")

    async def _drain_recent_pending_provisions() -> None:
        """Finish pending creates that lost in-flight tasks (boot + periodic).

        Bounded concurrency — never stampede the DB pool with thousands of tasks.
        """
        await asyncio.sleep(25)
        from sqlalchemy import text

        from app.services.orchestrator import _provision_account_now

        async def _wave(limit: int = 120) -> int:
            async with AsyncSessionLocal() as db:
                rows = await db.execute(
                    text(
                        """
                        SELECT pa.id
                        FROM panel_accounts pa
                        JOIN users u ON u.id = pa.user_id
                        WHERE pa.status IN ('pending', 'error')
                          AND u.created_at > now() - interval '12 hours'
                        ORDER BY pa.id DESC
                        LIMIT :lim
                        """
                    ),
                    {"lim": int(limit)},
                )
                ids = [int(r[0]) for r in rows.all()]
                await db.rollback()
            if not ids:
                return 0
            sem = asyncio.Semaphore(6)
            ok = 0

            async def _one(aid: int) -> None:
                nonlocal ok
                async with sem:
                    try:
                        if await _provision_account_now(aid):
                            ok += 1
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            "pending drain aid=%s failed", aid, exc_info=True
                        )

            await asyncio.gather(*(_one(i) for i in ids))
            logger.info(
                "pending provision drain n=%s ok=%s", len(ids), ok
            )
            return len(ids)

        # Boot wave, then keep draining every ~90s while recent pending remain.
        try:
            await _wave(120)
        except Exception:  # noqa: BLE001
            logger.exception("boot pending provision drain failed")
        while True:
            try:
                await asyncio.sleep(90)
                n = await _wave(100)
                if n == 0:
                    await asyncio.sleep(180)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("periodic pending provision drain failed")
                await asyncio.sleep(90)

    if run_bg:
        bg_tasks.append(
            asyncio.create_task(
                _drain_recent_pending_provisions(), name="astra-pending-drain"
            )
        )
    logger.info(
        "astra lifespan ready — serving; bg=%s tasks=%s",
        run_bg,
        len(bg_tasks),
    )
    try:
        yield
    finally:
        for task in bg_tasks:
            task.cancel()
        for task in bg_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
        await dispose_engine()


app = FastAPI(
    title=f"{settings.PANEL_NAME} Panel",
    version="1.0.0",
    description="Multi-source V2Ray panel with PasarGuard-compatible API",
    lifespan=lifespan,
)

_SUB_HOST_LANDING = """<!DOCTYPE html>
<html lang="fa" dir="rtl"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Subscription</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;font-family:system-ui,sans-serif;background:#07141e;color:#ecfaf6}
main{text-align:center;padding:24px;opacity:.85}
h1{font-size:1.1rem;font-weight:700;margin:0 0 8px}
p{margin:0;font-size:.9rem;color:#7fa39a}
</style></head>
<body><main><h1>دامنه اشتراک</h1><p>این آدرس فقط برای لینک ساب است، نه پنل مدیریت.</p></main></body>
</html>"""


class SubHostGuardMiddleware(BaseHTTPMiddleware):
    """On dedicated subscription hostname, never serve the admin SPA."""

    async def dispatch(self, request: Request, call_next):
        sub = (getattr(settings, "ASTRA_SUB_DOMAIN", None) or "").strip().lower()
        panel = (getattr(settings, "ASTRA_PANEL_DOMAIN", None) or "").strip().lower()
        if not sub or (panel and sub == panel):
            return await call_next(request)
        host = (request.headers.get("host") or "").split(":", 1)[0].strip().lower()
        if host != sub:
            return await call_next(request)
        path = request.url.path or "/"
        if path.startswith("/sub") or path in ("/health", "/openapi.json"):
            return await call_next(request)
        if path.startswith("/docs") or path.startswith("/redoc") or path.startswith("/api"):
            return Response(status_code=404)
        return HTMLResponse(_SUB_HOST_LANDING, status_code=200)


class SubSkipGZipMiddleware(GZipMiddleware):
    """v2rayNG / v2rayN / Streisand often read only the first ~16KB of gzip or
    HTTP/2 DATA. Compressing /sub turns a 200-config feed into ~30 visible
    servers until the user taps Update repeatedly."""

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path") or ""
            if path.startswith("/sub"):
                await self.app(scope, receive, send)
                return
        await super().__call__(scope, receive, send)


app.add_middleware(SubSkipGZipMiddleware, minimum_size=800)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list + ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SubHostGuardMiddleware)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(groups.router)
app.include_router(group_configs.router)
app.include_router(panels.router)
app.include_router(astra_outbounds.router)
app.include_router(templates.router)
app.include_router(api_keys.router)
app.include_router(settings_api.router)
app.include_router(workers_api.router)
app.include_router(config_locations.router)
app.include_router(config_orders.router)
app.include_router(config_lookup.router)
app.include_router(backup.router)
app.include_router(subscription.router)
app.include_router(system.router)
app.include_router(analytics.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


# Reserved path prefixes — never serve SPA here (API / Swagger / sub page)
_SPA_SKIP_PREFIXES = (
    "api",
    "sub",
    "docs",
    "redoc",
    "openapi.json",
    "health",
    "assets",
)


_FRONTEND = _resolve_frontend_dist()
if _FRONTEND is not None:
    logger.info("Serving frontend from %s", _FRONTEND)
    assets = _FRONTEND / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/")
    async def spa_index():
        return FileResponse(_FRONTEND / "index.html")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        from fastapi import HTTPException

        first = (full_path or "").split("/", 1)[0]
        # If these hit the catch-all, something overrode FastAPI routes — do not
        # return index.html (that makes React redirect "/" and hides Swagger).
        if first in _SPA_SKIP_PREFIXES or full_path in _SPA_SKIP_PREFIXES:
            raise HTTPException(status_code=404, detail="Not found")
        candidate = (_FRONTEND / full_path).resolve()
        try:
            candidate.relative_to(_FRONTEND)
        except ValueError:
            return FileResponse(_FRONTEND / "index.html")
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_FRONTEND / "index.html")
else:

    @app.get("/")
    async def root():
        return {"panel": settings.PANEL_NAME, "docs": "/docs", "version": "1.0.0"}
