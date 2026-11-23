from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, oauth2_scheme
from app.core.config import get_settings
from app.core.database import get_db, pool_status
from app.core.security import decode_token
from app.models.admin import Admin
from app.models.user import User
from app.services import update_ops
from app.services.host_metrics import collect_host_metrics
from app.services.version_info import get_version

router = APIRouter(tags=["System"])
settings = get_settings()


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


@router.get("/api/system")
async def system_stats(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
):
    """Dashboard stats — live DB aggregates (no stale cache)."""
    import time as _time

    from sqlalchemy import case, text

    from app.core.database import AsyncSessionLocal

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
    now_ts = int(now_wall)

    async with AsyncSessionLocal() as db:
        await get_current_admin(request, token, db)

        # One round-trip for users + panels + groups (keeps dashboard snappy).
        agg = (
            await db.execute(
                text(
                    """
                    SELECT
                      (SELECT count(*) FROM users) AS users_total,
                      (SELECT count(*) FROM users WHERE status = 'active') AS users_active,
                      (SELECT count(*) FROM users
                         WHERE status = 'expired'
                            OR (expire IS NOT NULL AND expire > 0 AND expire <= :now_ts)
                      ) AS users_expired,
                      (SELECT count(*) FROM users WHERE status = 'limited') AS users_limited,
                      (SELECT coalesce(sum(used_traffic), 0) FROM users) AS traffic,
                      (SELECT count(*) FROM panels) AS panels_total,
                      (SELECT count(*) FROM panels WHERE is_active IS TRUE) AS panels_active,
                      (SELECT count(*) FROM groups) AS groups_total
                    """
                ),
                {"now_ts": now_ts},
            )
        ).mappings().one()

        # Exclude capacity / transient timeouts from "real" sync errors.
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

        await db.rollback()
        return {
            "version": get_version(),
            "panel": settings.PANEL_NAME,
            "users_total": int(agg["users_total"] or 0),
            "users_active": int(agg["users_active"] or 0),
            "users_expired": int(agg["users_expired"] or 0),
            "users_limited": int(agg["users_limited"] or 0),
            "users_with_errors": int(err_row.get("users_err") or 0),
            "panels_total": int(agg["panels_total"] or 0),
            "panels_active": int(agg["panels_active"] or 0),
            "groups_total": int(agg["groups_total"] or 0),
            "sync_errors": int(err_row.get("sync_err") or 0),
            "total_used_traffic": int(agg["traffic"] or 0),
            "top_users": top_users,
            "update_supported": update_ops.available(),
        }


@router.get("/api/system/db-pool")
async def system_db_pool(_admin=Depends(get_current_admin)):
    """Live SQLAlchemy pool pressure — for ops / debugging panel freezes."""
    return {"ok": True, "pool": pool_status()}


@router.get("/api/system/host")
async def system_host(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
):
    """CPU / RAM / disk — no DB session held during host collection."""
    import asyncio

    from app.core.database import AsyncSessionLocal
    from app.services import host_metrics as hm

    # Auth in a short session, then release before /proc + systemctl I/O.
    async with AsyncSessionLocal() as db:
        await get_current_admin(request, token, db)
        await db.rollback()

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
