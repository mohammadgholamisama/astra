"""Admin analytics: overview + per-user behavior dossier."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin
from app.core.database import get_db
from app.models.user import User
from app.services import user_analytics as analytics_svc

router = APIRouter(tags=["Analytics"])


@router.get("/api/analytics/overview")
async def analytics_overview(
    limit: int = Query(40, ge=5, le=120),
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    return await analytics_svc.build_overview(db, limit_users=limit)


@router.get("/api/analytics/user/{username}")
async def analytics_user(
    username: str,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    user = (
        await db.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="کاربر پیدا نشد")
    return await analytics_svc.build_user_analytics(db, user)


@router.post("/api/analytics/sample-now")
async def analytics_sample_now(
    _admin=Depends(get_current_admin),
):
    """Force one analytics sample cycle (admin/debug)."""
    stats = await analytics_svc.sample_all_users_once()
    return {"ok": True, **stats}
