import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, ORJSONResponse, Response
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
    integration as integration_api,
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
from app.openapi_fa import OPENAPI_DESC, OPENAPI_TAGS, customize_openapi
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
# Quieter default — access log off at uvicorn; hush httpx/uvicorn access spam.
_log_level = (getattr(settings, "LOG_LEVEL", None) or "WARNING").upper()
try:
    logging.basicConfig(level=getattr(logging, _log_level, logging.WARNING))
except Exception:  # noqa: BLE001
    logging.basicConfig(level=logging.WARNING)
logging.getLogger("astra").setLevel(logging.INFO)
logging.getLogger("astra.worker").setLevel(logging.INFO)
logging.getLogger("astra.node").setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
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


async def _drain_recent_pending_provisions() -> None:
    """Finish pending/error creates — no age cutoff, partial users first.

    One broken panel used to cancel sibling creates; sticky pending then sat
    forever because the old drain only looked at 48h. This loop is the
    durable backfill for ۱۱/۲۶-style cards.
    """
    await asyncio.sleep(12)
    from app.services.orchestrator import drain_pending_panel_accounts

    try:
        boot = await drain_pending_panel_accounts(
            limit_accounts=800, prefer_partial_users=True
        )
        logger.info(
            "boot pending provision drain queued=%s ok_now=%s",
            boot.get("queued"),
            boot.get("ok_now"),
        )
    except Exception:  # noqa: BLE001
        logger.exception("boot pending provision drain failed")
    while True:
        try:
            await asyncio.sleep(12)
            stats = await drain_pending_panel_accounts(
                limit_accounts=700, prefer_partial_users=True
            )
            n = int(stats.get("queued") or 0)
            if n == 0:
                await asyncio.sleep(60)
            elif n >= 300:
                # Backlog still huge — keep chewing with short pause.
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("periodic pending provision drain failed")
            await asyncio.sleep(30)


async def _recent_create_complete_loop() -> None:
    """Brand-new sales must reach N/N fast — never sit behind the 50k backlog.

    Every ~12s: find users created in the last 90 minutes with pending/empty
    share and run ``complete_user_panel_coverage`` (clears circuit, parallel
    create, waits on leftovers).
    """
    await asyncio.sleep(18)
    from app.services.orchestrator import (
        complete_user_panel_coverage,
        find_recent_incomplete_user_ids,
    )

    while True:
        n_done = 0
        ids: list[int] = []
        try:
            ids = await find_recent_incomplete_user_ids(
                limit=12, max_age_minutes=90.0
            )
            for uid in ids:
                try:
                    stats = await complete_user_panel_coverage(
                        int(uid), max_rounds=3, round_budget=55.0
                    )
                    if stats.get("done"):
                        n_done += 1
                    else:
                        logger.warning(
                            "recent create still partial user_id=%s chip=%s/%s",
                            uid,
                            stats.get("ok"),
                            stats.get("total"),
                        )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "recent create complete failed user_id=%s", uid
                    )
            if ids:
                logger.info(
                    "recent create complete wave users=%s done=%s",
                    len(ids),
                    n_done,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("recent create complete loop failed")
        await asyncio.sleep(8 if (n_done or ids) else 14)


async def _empty_user_coverage_heal_loop() -> None:
    """Never leave users stuck at ۰/۰ or partial ۱۱/۲۶ پنل / empty /sub.

    Independent of AUTO_RETRY_ERRORS — create/bulk bg tasks can die on
    worker restart; this loop on ``astra-bg`` is the durable guarantee.
    """
    await asyncio.sleep(25)
    from app.services.orchestrator import (
        force_heal_zero_active_users,
        heal_recent_empty_users,
        heal_users_missing_panel_accounts,
        restamp_empty_share_actives,
    )

    while True:
        healed = 0
        try:
            # Active+/sub-only → pending so drain+create never skip ۱۷/۲۱ cards.
            # Small batches — large restamp+drain stampeded QueuePool (16+8).
            try:
                stamped = int(await restamp_empty_share_actives(limit_accounts=120) or 0)
                if stamped:
                    healed += 1
            except Exception:  # noqa: BLE001
                logger.debug("restamp empty-share skipped", exc_info=True)
            # ۰/N + partial empty-share first — previously starved behind partial drains.
            healed += int(await force_heal_zero_active_users(limit_users=12) or 0)
            healed += int(
                await heal_recent_empty_users(
                    limit_users=60, max_age_hours=168.0
                )
                or 0
            )
            # Steady chew — avoid pool stampede that blocked freeze/kill.
            healed += int(
                await heal_users_missing_panel_accounts(limit_users=60)
                or 0
            )
            if healed:
                logger.info("coverage heal loop healed=%s", healed)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("empty-user coverage heal loop failed")
        await asyncio.sleep(8 if healed else 18)


async def _exhausted_remote_kill_loop() -> None:
    """Never leave limited/expired users with live remotes after اتمام حجم.

    Independent of /sub traffic — chews unfrozen-blocked until every account
    is cut. Force bypasses kill cooldown so a prior failed wave retries.
    """
    await asyncio.sleep(20)
    from app.services import panel_usage_bulk as bulk_usage

    while True:
        killed = 0
        try:
            stats = await bulk_usage.heal_exhausted_and_unfrozen(limit=200)
            killed = (
                int(stats.get("kill") or 0)
                + int(stats.get("flipped") or 0)
                + int(stats.get("freeze_ok") or 0)
            )
            if killed or int(stats.get("unfrozen_left") or 0) > 0:
                logger.warning(
                    "exhausted-kill loop flipped=%s kill=%s enqueued=%s "
                    "freeze_ok=%s freeze_fail=%s unfrozen=%s left=%s",
                    stats.get("flipped"),
                    stats.get("kill"),
                    stats.get("enqueued"),
                    stats.get("freeze_ok"),
                    stats.get("freeze_fail"),
                    stats.get("unfrozen"),
                    stats.get("unfrozen_left"),
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("exhausted-kill loop failed")
        await asyncio.sleep(8 if killed else 20)


async def _astra_xray_heal_loop() -> None:
    """Keep astra-xray@N up — calm cadence; never stampede systemctl/D-Bus.

    ``ensure_all_astra_xray_running`` opens its own isolated
    ``AsyncSessionLocal`` (never shares a session with analytics / usage-report).
    """
    from app.services import astra_node as node

    await asyncio.sleep(90)
    while True:
        try:
            # Isolated helper — own short session inside; exceptions stay here.
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

            # When a Worker owns remotes, skip per-user HTTP pulls — only
            # recount from meters already written by usage-report.
            worker_ok = False
            try:
                from app.services.worker_client import any_worker_healthy

                worker_ok = bool(any_worker_healthy())
            except Exception:  # noqa: BLE001
                worker_ok = False
            ids = ids[:4] if (under_pressure or worker_ok) else ids[:10]
            if ids:
                # Never share a session across concurrent pulls. Each task opens
                # one isolated AsyncSessionLocal under Sem(8) — no nested
                # pool_safe checkout while that session is open.
                sem = asyncio.Semaphore(8)

                async def _one(uid: int) -> None:
                    async with sem:
                        try:
                            from sqlalchemy.orm.attributes import (
                                set_committed_value as _scv,
                            )

                            from app.models.panel import Panel as _Panel
                            from app.models.user import (
                                PanelAccount as _PA,
                                User as _User,
                            )
                            from app.services.orchestrator import (
                                activate_on_hold_if_used as _act,
                                enforce_user_quota as _enq,
                                recount_user_used_traffic as _rct,
                            )

                            async with AsyncSessionLocal() as local_db:
                                user = (
                                    await local_db.execute(
                                        select(_User).where(
                                            _User.id == int(uid)
                                        )
                                    )
                                ).scalar_one_or_none()
                                if user is None:
                                    return
                                accounts = list(
                                    (
                                        await local_db.execute(
                                            select(_PA).where(
                                                _PA.user_id == int(uid)
                                            )
                                        )
                                    )
                                    .scalars()
                                    .all()
                                )
                                if accounts:
                                    pids = list(
                                        {
                                            int(a.panel_id)
                                            for a in accounts
                                            if a.panel_id
                                        }
                                    )
                                    by_id: dict[int, _Panel] = {}
                                    if pids:
                                        for p in (
                                            await local_db.execute(
                                                select(_Panel).where(
                                                    _Panel.id.in_(pids)
                                                )
                                            )
                                        ).scalars().all():
                                            by_id[int(p.id)] = p
                                    for acc in accounts:
                                        panel = by_id.get(int(acc.panel_id))
                                        if panel is not None:
                                            _scv(acc, "panel", panel)
                                _scv(user, "panel_accounts", accounts)
                                await _rct(local_db, user, accounts)
                                await _act(local_db, user)
                                await _enq(local_db, user)
                                await local_db.commit()
                            # Always live-pull for hot/sub users — even when a
                            # Worker is healthy. Recount-only left meters stale
                            # whenever usage-report deltas were dropped.
                            if under_pressure and worker_ok:
                                try:
                                    from app.services import usage_priority as uprio

                                    await uprio.enqueue_user_remotes(
                                        int(uid), ttl=600.0
                                    )
                                except Exception:  # noqa: BLE001
                                    pass
                                return
                            await asyncio.wait_for(
                                refresh_usage_pool_safe(
                                    int(uid), force=True, quick=True
                                ),
                                timeout=14.0 if worker_ok else 20.0,
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
                    "hot usage: pulled=%s pressure=%s worker=%s dt=%.1fs",
                    len(ids),
                    int(under_pressure),
                    int(worker_ok),
                    _time.monotonic() - t0,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("hot usage loop failed")
        interval = max(30, int(getattr(settings, "USAGE_HOT_SECONDS", 45) or 45))
        try:
            from app.services.worker_client import any_worker_healthy

            # Worker path is cheap (cache-first) — keep hot cadence tight.
            if any_worker_healthy():
                interval = min(interval, 40)
        except Exception:  # noqa: BLE001
            pass
        elapsed = _time.monotonic() - t0
        await asyncio.sleep(max(20.0, float(interval) - elapsed))


async def _priority_usage_loop() -> None:
    """Dedicated lane for live consumers — never deferred by fleet/pool pressure.

    After check-usage / successful pull, users stay in the priority set ~5 min.
    When a Worker owns remote meters, only recount from DB (no per-user HTTP).
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
            worker_ok = False
            try:
                from app.services.worker_client import any_worker_healthy

                worker_ok = bool(any_worker_healthy())
            except Exception:  # noqa: BLE001
                worker_ok = False
            ids = list(take_live_usage_priority(360.0, 6))[:6]
            if ids:
                for uid in ids:
                    try:
                        # Always enqueue + live pull (worker cache-first is <100ms
                        # when fresh; never recount-only for live consumers).
                        if worker_ok:
                            try:
                                from app.services import usage_priority as uprio

                                await uprio.enqueue_user_remotes(
                                    int(uid), ttl=600.0
                                )
                            except Exception:  # noqa: BLE001
                                pass
                        await asyncio.wait_for(
                            refresh_usage_pool_safe(
                                int(uid), force=True, quick=True
                            ),
                            timeout=12.0 if worker_ok else 25.0,
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
                    "priority usage: pulled=%s worker=%s dt=%.1fs",
                    len(ids),
                    int(worker_ok),
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
    """Cadenced analytics snapshot; live Astra sync stays light.

    Live usage-report deltas already feed hour buckets via
    ``user_analytics.record_usage_deltas``. This loop is a backup sampler +
    panel refresh — keep it frequent but never hold ``background_db_sem``
    across the whole user scan (that starved the pool for minutes).
    """
    from app.core.database import AsyncSessionLocal, background_db_sem
    from app.services import panel_analytics as panel_analytics_svc
    from app.services import user_analytics as analytics_svc

    await asyncio.sleep(15)
    cycle = 0
    while True:
        try:
            stats = await analytics_svc.sample_all_users_once()
            panel_stats = await panel_analytics_svc.sample_all_panels_once()
            # Every ~2 minutes: live Xray sync so hour columns track consumption
            if cycle % 2 == 0:
                sem = background_db_sem()
                await sem.acquire()
                try:
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
            if cycle == 1 or cycle % 10 == 0:
                logger.info("analytics sample: users=%s panels=%s", stats, panel_stats)
            if cycle % 60 == 0:
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
        await asyncio.sleep(60)


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
            # Coalesced usage-report consumer (merges chunks per panel).
            await ensure_usage_report_worker()
            if run_bg:
                from app.services.worker_lease import ensure_lease_loop
                from app.services.usage_tiers import ensure_tier_loop

                await ensure_lease_loop()
                await ensure_tier_loop()
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

    if run_bg:
        bg_tasks.append(
            asyncio.create_task(
                _drain_recent_pending_provisions(), name="astra-pending-drain"
            )
        )
        bg_tasks.append(
            asyncio.create_task(
                _recent_create_complete_loop(),
                name="astra-recent-create-complete",
            )
        )
        bg_tasks.append(
            asyncio.create_task(
                _empty_user_coverage_heal_loop(),
                name="astra-empty-coverage-heal",
            )
        )
        bg_tasks.append(
            asyncio.create_task(
                _exhausted_remote_kill_loop(),
                name="astra-exhausted-kill",
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
    title="مستندات API · Astra",
    version="1.0.0",
    description=OPENAPI_DESC,
    lifespan=lifespan,
    default_response_class=ORJSONResponse,
    openapi_tags=OPENAPI_TAGS,
    docs_url=None,
    redoc_url=None,
    swagger_ui_parameters={
        # List all APIs for integrators; Try it out stays opt-in (no auto Execute).
        "docExpansion": "list",
        "defaultModelsExpandDepth": 1,
        "defaultModelExpandDepth": 2,
        "displayRequestDuration": True,
        "filter": True,
        "tryItOutEnabled": False,
        "persistAuthorization": True,
    },
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
app.include_router(integration_api.router)
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

# Wrap once so /docs shows Persian summaries without recursion.
_orig_openapi = app.openapi
_openapi_schema_fa: dict | None = None


def _openapi_with_fa():
    global _openapi_schema_fa
    if _openapi_schema_fa is not None:
        return _openapi_schema_fa
    app.openapi_schema = None
    schema = customize_openapi(_orig_openapi())
    _openapi_schema_fa = schema
    app.openapi_schema = schema
    return schema


app.openapi = _openapi_with_fa  # type: ignore[method-assign]


# Keep Swagger chrome LTR (arrows/chevrons stay correct). Only Persian text aligns right.
_DOCS_RTL_INJECT = (
    '<link rel="stylesheet" '
    'href="https://cdn.jsdelivr.net/gh/rastikerdar/vazirmatn@v33.003/Vazirmatn-font-face.css" />'
    "<style>"
    "body{font-family:Vazirmatn,Tahoma,sans-serif}"
    ".swagger-ui .opblock-summary-description,"
    ".swagger-ui .opblock-description-wrapper,"
    ".swagger-ui .opblock-tag,"
    ".swagger-ui .opblock-tag small,"
    ".swagger-ui .info .title,"
    ".swagger-ui .info p,"
    ".swagger-ui .info li,"
    ".swagger-ui .markdown p,"
    ".swagger-ui .renderedMarkdown p{text-align:right;direction:rtl}"
    ".swagger-ui .markdown pre,"
    ".swagger-ui .markdown code,"
    ".swagger-ui .renderedMarkdown pre,"
    ".swagger-ui .renderedMarkdown code,"
    ".swagger-ui .highlight-code,"
    ".swagger-ui .microlight{direction:ltr;text-align:left}"
    ".swagger-ui .opblock-summary-method,"
    ".swagger-ui .opblock-summary-path,"
    ".swagger-ui .opblock-summary-path__unsafe-disable-unique-id,"
    ".swagger-ui table,"
    ".swagger-ui .parameters,"
    ".swagger-ui .responses-wrapper,"
    ".swagger-ui .model-example,"
    ".swagger-ui .example{direction:ltr;text-align:left}"
    ".swagger-ui .expand-operation svg,"
    ".swagger-ui .expand-methods svg,"
    ".swagger-ui .arrow,"
    ".swagger-ui .opblock-control-arrow{transform:none!important}"
    ".swagger-ui .auth-wrapper .authorize{float:none}"
    "</style>"
)


@app.get("/docs", include_in_schema=False)
async def swagger_docs():
    from fastapi.openapi.docs import get_swagger_ui_html

    html = get_swagger_ui_html(
        openapi_url='/openapi.json',
        title='Astra API Docs',
        swagger_ui_parameters={
            # All tags/APIs visible for developers; Execute never runs until Try it out.
            "docExpansion": "list",
            "defaultModelsExpandDepth": 1,
            "defaultModelExpandDepth": 2,
            "displayRequestDuration": True,
            "filter": True,
            "tryItOutEnabled": False,
            "persistAuthorization": True,
        },
    )
    body = (
        html.body.decode("utf-8")
        if isinstance(html.body, (bytes, bytearray))
        else str(html.body)
    )
    if "</head>" in body:
        body = body.replace("</head>", _DOCS_RTL_INJECT + "</head>", 1)
    else:
        body = _DOCS_RTL_INJECT + body
    return HTMLResponse(body)


@app.get("/redoc", include_in_schema=False)
async def redoc_docs():
    from fastapi.openapi.docs import get_redoc_html

    return get_redoc_html(
        openapi_url='/openapi.json',
        title='Astra API Docs',
    )


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
