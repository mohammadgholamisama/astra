"""Admin analytics: overview + per-user / per-panel dossiers."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_admin
from app.core.database import get_db
from app.models.user import PanelAccount, User
from app.services import panel_analytics as panel_analytics_svc
from app.services import user_analytics as analytics_svc

router = APIRouter(tags=["Analytics"])
logger = logging.getLogger("astra.analytics.api")


@router.get("/api/analytics/overview")
async def analytics_overview(
    limit: int = Query(40, ge=5, le=120),
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    return await analytics_svc.build_overview(db, limit_users=limit)


@router.get("/api/analytics/users/search")
async def analytics_users_search(
    q: str = Query("", max_length=64),
    limit: int = Query(12, ge=1, le=30),
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Typeahead for analytics user search."""
    needle = (q or "").strip()
    if len(needle) < 1:
        return {"items": []}
    like = f"%{needle}%"
    rows = (
        await db.execute(
            select(User.username, User.display_name, User.status)
            .where(
                or_(
                    User.username.ilike(like),
                    func.coalesce(User.display_name, "").ilike(like),
                )
            )
            .order_by(User.username.asc())
            .limit(limit)
        )
    ).all()
    return {
        "items": [
            {
                "username": uname,
                "display_name": dname,
                "status": getattr(st, "value", st),
            }
            for uname, dname, st in rows
        ]
    }


async def _fresh_sample_user(db: AsyncSession, user: User) -> User:
    """Pull live remote usage then advance analytics cursors for this user."""
    from app.services.orchestrator import refresh_usage

    try:
        await refresh_usage(db, user, force=True, quick=True)
        await db.flush()
    except Exception:  # noqa: BLE001
        logger.warning(
            "analytics fresh refresh failed user=%s", user.username, exc_info=True
        )
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        # Reload after rollback
        user = (
            await db.execute(select(User).where(User.id == int(user.id)))
        ).scalar_one_or_none() or user

    # Sum raw per panel for sampling
    acc_rows = (
        await db.execute(
            select(
                PanelAccount.panel_id,
                func.coalesce(func.sum(PanelAccount.used_traffic), 0),
            )
            .where(PanelAccount.user_id == int(user.id))
            .group_by(PanelAccount.panel_id)
        )
    ).all()
    total = 0
    for pid, raw in acc_rows:
        raw_i = int(raw or 0)
        total += raw_i
        try:
            await panel_analytics_svc.apply_panel_user_sample(
                db, int(user.id), int(pid), raw_i
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "panel user sample failed u=%s p=%s", user.id, pid, exc_info=True
            )
    try:
        await analytics_svc.apply_raw_sample(db, int(user.id), total)
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning(
            "analytics fresh sample commit failed user=%s",
            getattr(user, "username", "?"),
            exc_info=True,
        )
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass

    fresh = (
        await db.execute(
            select(User)
            .where(User.id == int(user.id))
            .options(selectinload(User.panel_accounts))
        )
    ).scalar_one_or_none()
    return fresh or user


@router.get("/api/analytics/user/{username}")
async def analytics_user(
    username: str,
    refresh: bool = Query(
        True,
        description="Pull live usage + sample analytics before returning",
    ),
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    user = (
        await db.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="کاربر پیدا نشد")
    if refresh:
        user = await _fresh_sample_user(db, user)
    return await analytics_svc.build_user_analytics(db, user)


@router.get("/api/analytics/panels")
async def analytics_panels(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Overview of all panels: raw usage, online, operators, test quality."""
    return await panel_analytics_svc.build_panel_overview(db)


@router.get("/api/analytics/panels/{panel_id}")
async def analytics_panel_detail(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    try:
        return await panel_analytics_svc.build_panel_detail(db, panel_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="پنل پیدا نشد") from None


@router.post("/api/analytics/sample-now")
async def analytics_sample_now(
    _admin=Depends(get_current_admin),
):
    """Force one analytics sample cycle (admin/debug)."""
    stats = await analytics_svc.sample_all_users_once()
    panel_stats = await panel_analytics_svc.sample_all_panels_once()
    return {"ok": True, "users": stats, "panels": panel_stats}
