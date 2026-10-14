import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    api_keys,
    auth,
    backup,
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
from app.models import group_config as _group_config_model  # noqa: F401
from app.models import site_setting as _site_setting_model  # noqa: F401
from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.core.schema_sync import ensure_schema
from app.seed import seed_admin
from app.services.site_settings import refresh_cache as refresh_site_settings

settings = get_settings()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("astra")


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
    yield


app = FastAPI(
    title=f"{settings.PANEL_NAME} Panel",
    version="1.0.0",
    description="Multi-source V2Ray panel with PasarGuard-compatible API",
    lifespan=lifespan,
)

app.add_middleware(GZipMiddleware, minimum_size=800)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list + ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(groups.router)
app.include_router(group_configs.router)
app.include_router(panels.router)
app.include_router(templates.router)
app.include_router(api_keys.router)
app.include_router(settings_api.router)
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
