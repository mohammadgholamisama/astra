from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, oauth2_scheme
from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import decode_token
from app.models.admin import Admin
from app.models.group import Group
from app.models.panel import Panel
from app.models.user import PanelAccount, PanelAccountStatus, User, UserStatus
from app.services import update_ops
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
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    import time as _time

    now_ts = int(_time.time())
    users_total = (await db.execute(select(func.count(User.id)))).scalar() or 0
    users_active = (
        await db.execute(select(func.count(User.id)).where(User.status == UserStatus.active))
    ).scalar() or 0
    users_expired = (
        await db.execute(
            select(func.count(User.id)).where(
                or_(
                    User.status == UserStatus.expired,
                    and_(
                        User.expire.is_not(None),
                        User.expire > 0,
                        User.expire <= now_ts,
                    ),
                )
            )
        )
    ).scalar() or 0
    users_limited = (
        await db.execute(
            select(func.count(User.id)).where(User.status == UserStatus.limited)
        )
    ).scalar() or 0
    # Distinct users that still have an error on an active panel
    users_with_errors = (
        await db.execute(
            select(func.count(func.distinct(PanelAccount.user_id)))
            .select_from(PanelAccount)
            .join(Panel, Panel.id == PanelAccount.panel_id)
            .where(
                PanelAccount.status == PanelAccountStatus.error,
                Panel.is_active.is_(True),
            )
        )
    ).scalar() or 0
    panels_total = (await db.execute(select(func.count(Panel.id)))).scalar() or 0
    panels_active = (
        await db.execute(select(func.count(Panel.id)).where(Panel.is_active.is_(True)))
    ).scalar() or 0
    groups_total = (await db.execute(select(func.count(Group.id)))).scalar() or 0
    sync_errors = (
        await db.execute(
            select(func.count(PanelAccount.id)).where(PanelAccount.status == PanelAccountStatus.error)
        )
    ).scalar() or 0
    traffic = (await db.execute(select(func.coalesce(func.sum(User.used_traffic), 0)))).scalar() or 0

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

    return {
        "version": get_version(),
        "panel": settings.PANEL_NAME,
        "users_total": users_total,
        "users_active": users_active,
        "users_expired": int(users_expired),
        "users_limited": int(users_limited),
        "users_with_errors": int(users_with_errors),
        "panels_total": panels_total,
        "panels_active": panels_active,
        "groups_total": groups_total,
        "sync_errors": sync_errors,
        "total_used_traffic": int(traffic),
        "top_users": top_users,
        "update_supported": update_ops.available(),
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
