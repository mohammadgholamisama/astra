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
    api_keys,
    astra_outbounds,
    auth,
    backup,
    config_locations,
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
from app.models.user import PanelAccount, User, UserStatus
from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.core.schema_sync import ensure_schema
from app.seed import seed_admin
from app.services.config_hides import refresh_cache as refresh_config_hides
from app.services.config_locations import refresh_cache as refresh_config_locations
from app.services.config_orders import refresh_cache as refresh_config_orders
from app.services.site_settings import refresh_cache as refresh_site_settings

settings = get_settings()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("astra")


async def _heal_failed_panels_loop() -> None:
    """Periodically recreate users on panels that were unreachable at create time.

    When a dead panel comes back, error/pending accounts become active and
    their share links show up on the subscription automatically.
    """
    from app.services.orchestrator import heal_failed_panel_accounts_batch

    await asyncio.sleep(20)
    while True:
        try:
            n = await heal_failed_panel_accounts_batch(limit_users=30)
            if n:
                logger.info("heal loop: recovered panels for %s users", n)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("heal failed panels loop failed")
        await asyncio.sleep(45)


async def _outbound_health_loop() -> None:
    """Re-ping Astra outbounds so recovered ones reappear in subscription.

    When an outbound fails ping it is dropped from share-links; without this
    loop it stayed missing until an admin clicked Ping even after recovery.
    """
    from app.models.panel import Panel, PanelType
    from app.services import astra_node as node

    await asyncio.sleep(45)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Panel).where(
                        Panel.type == PanelType.astra,
                        Panel.is_active.is_(True),
                    )
                )
                panels = list(result.scalars().all())
                for panel in panels:
                    try:
                        stats = await node.reconcile_outbounds_health(
                            db, panel, concurrency=3
                        )
                        if stats.get("changed"):
                            logger.info(
                                "outbound health: panel=%s pinged=%s changed=%s ok=%s",
                                panel.id,
                                stats.get("pinged"),
                                stats.get("changed"),
                                stats.get("ok"),
                            )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "outbound health reconcile failed panel=%s", panel.id
                        )
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("outbound health loop failed")
        await asyncio.sleep(120)


async def _usage_sync_loop() -> None:
    """Every ~10s: sync Astra traffic, detect quota end, freeze ALL remotes.

    Does not depend on an admin opening the panel — exhausted users are
    disabled on Marzban/PasarGuard/Sanaei/Rebecca/Astra as soon as the watch
    cycle sees used ≥ limit.
    """
    import time as _time

    from app.services.orchestrator import run_quota_watch_cycle

    await asyncio.sleep(5)
    while True:
        t0 = _time.monotonic()
        try:
            stats = await run_quota_watch_cycle()
            if stats.get("flipped") or stats.get("freeze_ok") or stats.get("freeze_fail"):
                logger.info(
                    "quota watch: astra=%s checked=%s flipped=%s freeze_ok=%s freeze_fail=%s pulled=%s",
                    stats.get("astra_panels"),
                    stats.get("checked"),
                    stats.get("flipped"),
                    stats.get("freeze_ok"),
                    stats.get("freeze_fail"),
                    stats.get("usage_pulled"),
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("quota watch loop failed")
        # Target cadence ≈ USAGE_REFRESH_SECONDS (default 10)
        interval = max(5, int(settings.USAGE_REFRESH_SECONDS or 10))
        elapsed = _time.monotonic() - t0
        await asyncio.sleep(max(1.0, interval - elapsed))


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
    usage_task = asyncio.create_task(_usage_sync_loop(), name="astra-usage-sync")
    heal_task = asyncio.create_task(
        _heal_failed_panels_loop(), name="astra-heal-panels"
    )
    outbound_task = asyncio.create_task(
        _outbound_health_loop(), name="astra-outbound-health"
    )
    try:
        yield
    finally:
        usage_task.cancel()
        heal_task.cancel()
        outbound_task.cancel()
        for task in (usage_task, heal_task, outbound_task):
            try:
                await task
            except asyncio.CancelledError:
                pass


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
app.include_router(backup.router)
app.include_router(subscription.router)
app.include_router(system.router)


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
