from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, oauth2_scheme
from app.core.config import get_settings
from app.core.database import get_db, pool_status
from app.core.security import decode_token
from app.models.admin import Admin
from app.models.group import Group
from app.models.panel import Panel
from app.models.user import PanelAccount, PanelAccountStatus, User, UserStatus
from app.services import update_ops
from app.services.host_metrics import collect_host_metrics
from app.services.version_info import get_version

router = APIRouter(tags=["System"])
settings = get_settings()

# Shared short TTL (file) so multi-worker uvicorn all hit a warm dashboard.
_SYSTEM_CACHE: dict[str, object] = {"at": 0.0, "payload": None}
_SYSTEM_TTL_SEC = 12.0
_SYSTEM_CACHE_PATH = "/tmp/astra_system_stats.json"


async def _session_admin_only(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> Admin:
    if request.headers.get("X-Api-Key"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "SESSION_REQUIRED", "message": "فقط با ورود به پنل"},
        )
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("apikey "):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "SESSION_REQUIRED", "message": "فقط با ورود به پنل"},
        )
    bearer = token
    if not bearer and auth.lower().startswith("bearer "):
        bearer = auth[7:].strip()
    if not bearer:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    payload = decode_token(bearer)
    if not payload or "sub" not in payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    result = await db.execute(select(Admin).where(Admin.username == payload["sub"]))
    admin = result.scalar_one_or_none()
    if not admin or not admin.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin not found")
    return admin


def _read_system_cache(now_wall: float, now_mono: float):
    import json as _json
    from pathlib import Path as _Path

    cached = _SYSTEM_CACHE.get("payload")
    if cached is not None and (now_mono - float(_SYSTEM_CACHE["at"] or 0)) < _SYSTEM_TTL_SEC:
        return cached
    try:
        p = _Path(_SYSTEM_CACHE_PATH)
        if p.is_file():
            age = now_wall - p.stat().st_mtime
            if 0 <= age < _SYSTEM_TTL_SEC:
                disk = _json.loads(p.read_text(encoding="utf-8"))
                if isinstance(disk, dict) and "users_total" in disk:
                    _SYSTEM_CACHE["at"] = now_mono
                    _SYSTEM_CACHE["payload"] = disk
                    return disk
    except Exception:  # noqa: BLE001
        return None
    return None


def _write_system_cache(payload: dict, now_mono: float) -> None:
    import json as _json
    from pathlib import Path as _Path

    _SYSTEM_CACHE["at"] = now_mono
    _SYSTEM_CACHE["payload"] = payload
    try:
        _Path(_SYSTEM_CACHE_PATH).write_text(
            _json.dumps(payload, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass


@router.get("/api/system")
async def system_stats(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
):
    """Dashboard stats — cache hit skips DB entirely (no pool wait)."""
    import time as _time

    from sqlalchemy import case, text

    from app.core.database import AsyncSessionLocal

    # Light JWT check only — full admin row lookup happens on cache miss.
    bearer = token
    auth = request.headers.get("Authorization") or ""
    if not bearer and auth.lower().startswith("bearer "):
        bearer = auth[7:].strip()
    api_key = request.headers.get("X-Api-Key")
    if not bearer and not api_key and not auth.lower().startswith("apikey "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    if bearer:
        payload_tok = decode_token(bearer)
        if not payload_tok or "sub" not in payload_tok:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    now_wall = _time.time()
    now_mono = _time.monotonic()
    hit = _read_system_cache(now_wall, now_mono)
    if hit is not None:
        return hit

    async with AsyncSessionLocal() as db:
        # Validate admin once when rebuilding cache
        await get_current_admin(request, token, db)

        now_ts = int(now_wall)

        # One scan for user counters + traffic
        user_row = (
            await db.execute(
                select(
                    func.count(User.id),
                    func.coalesce(
                        func.sum(
                            case((User.status == UserStatus.active, 1), else_=0)
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    or_(
                                        User.status == UserStatus.expired,
                                        and_(
                                            User.expire.is_not(None),
                                            User.expire > 0,
                                            User.expire <= now_ts,
                                        ),
                                    ),
                                    1,
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            case((User.status == UserStatus.limited, 1), else_=0)
                        ),
                        0,
                    ),
                    func.coalesce(func.sum(User.used_traffic), 0),
                )
            )
        ).one()
        users_total = int(user_row[0] or 0)
        users_active = int(user_row[1] or 0)
        users_expired = int(user_row[2] or 0)
        users_limited = int(user_row[3] or 0)
        traffic = int(user_row[4] or 0)

        panel_row = (
            await db.execute(
                select(
                    func.count(Panel.id),
                    func.coalesce(
                        func.sum(case((Panel.is_active.is_(True), 1), else_=0)),
                        0,
                    ),
                )
            )
        ).one()
        panels_total = int(panel_row[0] or 0)
        panels_active = int(panel_row[1] or 0)

        groups_total = int(
            (await db.execute(select(func.count(Group.id)))).scalar() or 0
        )

        # Exclude capacity / transient timeouts from "real" sync errors.
        # Use portable LIKE (SQLite has no Postgres !~* operator).
        err_row = (
            await db.execute(
                text(
                    """
                    SELECT
                      count(DISTINCT pa.user_id) AS users_err,
                      count(pa.id) AS sync_err
                    FROM panel_accounts pa
                    JOIN panels p ON p.id = pa.panel_id
                    WHERE pa.status = 'error'
                      AND p.is_active IS TRUE
                      AND lower(COALESCE(pa.error_message, '')) NOT LIKE '%user limit%'
                      AND lower(COALESCE(pa.error_message, '')) NOT LIKE '%limit reached%'
                      AND COALESCE(pa.error_message, '') NOT LIKE '%ظرفیت%'
                      AND lower(COALESCE(pa.error_message, '')) NOT LIKE '%timeout%'
                      AND lower(COALESCE(pa.error_message, '')) NOT LIKE '%timed out%'
                      AND lower(COALESCE(pa.error_message, '')) NOT LIKE '%waiting for panel%'
                      AND COALESCE(pa.error_message, '') NOT LIKE '%طول کشید%'
                      AND COALESCE(pa.error_message, '') NOT LIKE '%تایم%'
                    """
                )
            )
        ).mappings().first() or {}
        users_with_errors = int(err_row.get("users_err") or 0)
        sync_errors = int(err_row.get("sync_err") or 0)

        top_rows = (
            await db.execute(
                select(
                    User.username,
                    User.display_name,
                    User.used_traffic,
                    User.data_limit,
                    User.status,
                    User.expire,
                )
                .where(User.used_traffic > 0)
                .order_by(User.used_traffic.desc())
                .limit(8)
            )
        ).all()
        top_users = [
            {
                "username": r.username,
                "display_name": r.display_name,
                "used_traffic": int(r.used_traffic or 0),
                "data_limit": int(r.data_limit or 0),
                "status": getattr(r.status, "value", r.status),
                "expire": int(r.expire or 0) if r.expire else 0,
            }
            for r in top_rows
        ]

        payload = {
            "version": get_version(),
            "panel": settings.PANEL_NAME,
            "users_total": users_total,
            "users_active": users_active,
            "users_expired": users_expired,
            "users_limited": users_limited,
            "users_with_errors": users_with_errors,
            "panels_total": panels_total,
            "panels_active": panels_active,
            "groups_total": groups_total,
            "sync_errors": sync_errors,
            "total_used_traffic": traffic,
            "top_users": top_users,
            "update_supported": update_ops.available(),
        }
        _write_system_cache(payload, now_mono)
        return payload


@router.get("/api/system/db-pool")
async def system_db_pool(_admin=Depends(get_current_admin)):
    """Live SQLAlchemy pool pressure — for ops / debugging panel freezes."""
    return {"ok": True, "pool": pool_status()}


@router.get("/api/system/host")
async def system_host(_admin=Depends(get_current_admin)):
    """CPU / RAM / disk / uptime / services for the dashboard (cached, non-blocking)."""
    import asyncio

    from app.services import host_metrics as hm

    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(collect_host_metrics),
            timeout=2.5,
        )
        if isinstance(data, dict):
            data = {**data, "db_pool": pool_status()}
        return data
    except asyncio.TimeoutError:
        if hm._CACHE:
            return {
                **hm._CACHE,
                "db_pool": pool_status(),
                "note": "پاسخ کند — آخرین نمونهٔ کش‌شده",
            }
        return {
            "ok": False,
            "engine": "timeout",
            "hostname": "",
            "os": "",
            "arch": "",
            "python": "",
            "collected_at": 0,
            "cpu_percent": 0,
            "cpu_count_logical": 0,
            "cpu_per_core": [],
            "load_avg": [0, 0, 0],
            "memory": {"total": 0, "used": 0, "free": 0, "percent": 0},
            "swap": {"total": 0, "used": 0, "free": 0, "percent": 0},
            "disks": [],
            "db_pool": pool_status(),
            "uptime_seconds": 0,
            "boot_time": 0,
            "process_count": 0,
            "net": {
                "bytes_sent": 0,
                "bytes_recv": 0,
                "packets_sent": 0,
                "packets_recv": 0,
            },
            "services": [],
            "note": "جمع‌آوری وضعیت سرور طول کشید — دوباره تلاش کنید",
        }


@router.get("/api/system/update")
async def check_system_update(_admin: Admin = Depends(_session_admin_only)):
    return await update_ops.check_update()


@router.post("/api/system/update")
async def apply_system_update(_admin: Admin = Depends(_session_admin_only)):
    try:
        return await update_ops.apply_update()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "UPDATE_UNAVAILABLE", "message": str(exc)},
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "UPDATE_FAILED", "message": str(exc)},
        ) from exc
