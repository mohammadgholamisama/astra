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
    """Every 3 minutes: same as admin «تلاش مجدد همه خطاها».

    When panels come back, error/pending accounts are retried automatically
    without waiting for someone to open the Users page.
    """
    from sqlalchemy import func, select

    from app.core.database import AsyncSessionLocal, background_db_sem
    from app.models.panel import Panel
    from app.models.user import PanelAccount, PanelAccountStatus
    from app.services.orchestrator import (
        heal_users_missing_panel_accounts,
        retry_all_failed_accounts,
    )

    await asyncio.sleep(75)
    while True:
        try:
            async with AsyncSessionLocal() as db:
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
                            )
                        )
                    )
                    or 0
                )
            if n_err > 0:
                logger.info(
                    "auto retry-errors: %s error/pending accounts — starting",
                    n_err,
                )
                sem = background_db_sem()
                await sem.acquire()
                try:
                    async with AsyncSessionLocal() as db:
                        stats = await retry_all_failed_accounts(
                            db,
                            overall_timeout=160.0,
                            per_remote_timeout=20.0,
                            probe_timeout=10.0,
                            concurrency=18,
                            limit_accounts=None,
                        )
                        try:
                            await db.commit()
                        except Exception:  # noqa: BLE001
                            pass
                        logger.info(
                            "auto retry-errors done attempted=%s recovered=%s remaining=%s down=%s",
                            stats.get("attempted"),
                            stats.get("recovered"),
                            stats.get("remaining"),
                            (stats.get("panels_down") or [])[:5],
                        )
                    try:
                        await heal_users_missing_panel_accounts(limit_users=60)
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
        await asyncio.sleep(180)



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
        await asyncio.sleep(180)


async def _usage_sync_loop() -> None:
    """Cadenced quota watch: sync Astra traffic, freeze exhausted remotes.

    Important: if a cycle is heavy (thousands of accounts), do NOT immediately
    re-enter — that was pegging CPU at ~100%. Always cool down at least
    ``USAGE_REFRESH_SECONDS``, and longer after slow cycles.
    """
    import time as _time

    from app.core.database import background_db_sem
    from app.services.orchestrator import run_quota_watch_cycle

    await asyncio.sleep(8)
    while True:
        t0 = _time.monotonic()
        try:
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
        interval = max(8, int(settings.USAGE_REFRESH_SECONDS or 8))
        # Heavy cycle → proportional cooldown so the event loop / DB can breathe
        if elapsed > interval:
            cooldown = min(90.0, max(float(interval), elapsed * 0.75))
        else:
            cooldown = max(1.0, float(interval) - elapsed)
        await asyncio.sleep(cooldown)


async def _analytics_sample_loop() -> None:
    """Every ~60s: snapshot usage; every ~3 min: live Astra sync into panel stats."""
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
    await ensure_schema()
    await seed_admin()
    async with AsyncSessionLocal() as db:
        await refresh_site_settings(db)
        await refresh_config_locations(db)
        await refresh_config_hides(db)
        await refresh_config_orders(db)
        # Undo mass hides from older ping-rank bug (once per install)
        from app.services.config_hides import clear_stale_ping_auto_hides_once
        from app.services.config_orders import (
            relayout_order_slots_once,
            seed_order_kind_from_folders_once,
        )

        await clear_stale_ping_auto_hides_once(db)
    # Never block panel boot on order seed/relayout — site must serve data first
    async def _bg_order_kind_boot() -> None:
        try:
            async with AsyncSessionLocal() as s:
                await seed_order_kind_from_folders_once(s)
                await relayout_order_slots_once(s)
        except Exception:  # noqa: BLE001
            logging.getLogger("astra").exception("order_kind boot task failed")

    usage_task = asyncio.create_task(_usage_sync_loop(), name="astra-usage-sync")
    analytics_task = asyncio.create_task(
        _analytics_sample_loop(), name="astra-analytics-sample"
    )
    retry_errors_task = asyncio.create_task(
        _auto_retry_errors_loop(), name="astra-auto-retry-errors"
    )
    outbound_task = asyncio.create_task(
        _outbound_health_loop(), name="astra-outbound-health"
    )
    order_boot_task = asyncio.create_task(
        _bg_order_kind_boot(), name="astra-order-kind-boot"
    )
    pool_task = asyncio.create_task(pool_guardian_loop(), name="astra-db-pool-guard")
    try:
        yield
    finally:
        usage_task.cancel()
        analytics_task.cancel()
        retry_errors_task.cancel()
        outbound_task.cancel()
        order_boot_task.cancel()
        pool_task.cancel()
        for task in (
            usage_task,
            analytics_task,
            retry_errors_task,
            outbound_task,
            order_boot_task,
            pool_task,
        ):
            try:
                await task
            except asyncio.CancelledError:
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
