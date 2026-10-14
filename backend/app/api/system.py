from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin
from app.core.config import get_settings
from app.core.database import get_db
from app.models.group import Group
from app.models.panel import Panel
from app.models.user import PanelAccount, PanelAccountStatus, User, UserStatus

router = APIRouter(tags=["System"])
settings = get_settings()


@router.get("/api/system")
async def system_stats(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    users_total = (await db.execute(select(func.count(User.id)))).scalar() or 0
    users_active = (
        await db.execute(select(func.count(User.id)).where(User.status == UserStatus.active))
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

    return {
        "version": "1.0.0",
        "panel": settings.PANEL_NAME,
        "users_total": users_total,
        "users_active": users_active,
        "panels_total": panels_total,
        "panels_active": panels_active,
        "groups_total": groups_total,
        "sync_errors": sync_errors,
        "total_used_traffic": int(traffic),
    }
