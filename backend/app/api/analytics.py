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
    tz_offset: int = Query(
        0,
        ge=-840,
        le=840,
        description="JS Date.getTimezoneOffset() minutes",
    ),
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    return await analytics_svc.build_overview(
        db, limit_users=limit, tz_offset_min=tz_offset
    )


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

    # Sum raw per panel for sampling (user + panel cursors)
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
    panel_online: dict[int, int] = {}
    for pid, raw in acc_rows:
        raw_i = int(raw or 0)
        total += raw_i
        try:
            is_on = await panel_analytics_svc.apply_panel_user_sample(
                db, int(user.id), int(pid), raw_i
            )
            if is_on:
                panel_online[int(pid)] = 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "panel user sample failed u=%s p=%s", user.id, pid, exc_info=True
            )
    # Also advance each affected panel's raw cursor from live totals
    try:
        live_by_panel = await panel_analytics_svc.live_panel_totals(db)
        for pid, _raw in acc_rows:
            pid_i = int(pid)
            try:
                await panel_analytics_svc.apply_panel_raw_sample(
                    db,
                    pid_i,
                    int(live_by_panel.get(pid_i, 0)),
                    online_n=int(panel_online.get(pid_i, 0)),
                )
            except Exception:  # noqa: BLE001
                logger.debug("panel raw sample failed p=%s", pid_i, exc_info=True)
    except Exception:  # noqa: BLE001
        logger.debug("panel live totals after user refresh failed", exc_info=True)
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
    tz_offset: int = Query(0, ge=-840, le=840),
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
    return await analytics_svc.build_user_analytics(
        db, user, tz_offset_min=tz_offset
    )


@router.get("/api/analytics/panels")
async def analytics_panels(
    sample: bool = Query(
        False,
        description="Best-effort local sample first (never blocks if busy)",
    ),
    tz_offset: int = Query(0, ge=-840, le=840),
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Overview of all panels: raw usage, online, operators, test quality."""
    import asyncio

    if sample:
        try:
            await panel_analytics_svc.sample_all_panels_once(force=False)
        except Exception:  # noqa: BLE001
            logger.debug("panel sample before overview failed", exc_info=True)
    try:
        # Page load stays light — Xray sync happens on «تازه‌سازی زنده»
        return await asyncio.wait_for(
            panel_analytics_svc.build_panel_overview(
                db, tz_offset_min=tz_offset, sync_astra=False, persist=True
            ),
            timeout=8.0,
        )
    except asyncio.TimeoutError as exc:
        logger.warning("build_panel_overview timed out")
        try:
            return await panel_analytics_svc.build_panel_overview(
                db, tz_offset_min=tz_offset, sync_astra=False, persist=False
            )
        except Exception as exc2:  # noqa: BLE001
            logger.exception("build_panel_overview fallback failed")
            raise HTTPException(
                status_code=504, detail="آمار پنل‌ها طول کشید — دوباره تلاش کنید"
            ) from exc2
    except Exception as exc:  # noqa: BLE001
        logger.exception("build_panel_overview failed")
        raise HTTPException(
            status_code=500, detail="خطا در ساخت آمار پنل‌ها"
        ) from exc


@router.post("/api/analytics/panels/refresh")
async def analytics_panels_refresh(
    tz_offset: int = Query(0, ge=-840, le=840),
    _admin=Depends(get_current_admin),
):
    """Fast Astra live refresh — hard-capped to a few seconds."""
    import asyncio

    try:
        return await asyncio.wait_for(
            panel_analytics_svc.refresh_panel_analytics_fast(
                tz_offset_min=tz_offset, budget_s=8.0
            ),
            timeout=10.0,
        )
    except asyncio.TimeoutError as exc:
        # Never leave the UI hanging — return last known overview quickly
        from app.core.database import AsyncSessionLocal

        try:
            async with AsyncSessionLocal() as db:
                overview = await panel_analytics_svc.build_panel_overview(
                    db,
                    tz_offset_min=tz_offset,
                    sync_astra=False,
                    persist=False,
                )
            return {
                "ok": True,
                "stats": {
                    "astra_synced": 0,
                    "astra_only": True,
                    "users_pulled": 0,
                    "bytes_added": 0,
                    "ms": 10000,
                    "error": "timeout",
                },
                "overview": overview,
            }
        except Exception:  # noqa: BLE001
            raise HTTPException(
                status_code=504,
                detail="تازه‌سازی بیش از حد طول کشید — دوباره تلاش کنید",
            ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("panel analytics fast refresh failed")
        raise HTTPException(
            status_code=500, detail="خطا در تازه‌سازی آمار پنل‌ها"
        ) from exc


@router.get("/api/analytics/panels/{panel_id}")
async def analytics_panel_detail(
    panel_id: int,
    refresh: bool = Query(False),
    tz_offset: int = Query(0, ge=-840, le=840),
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    if refresh:
        await panel_analytics_svc.refresh_panels_live(pull_remotes=True)
    try:
        return await panel_analytics_svc.build_panel_detail(
            db, panel_id, tz_offset_min=tz_offset
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="پنل پیدا نشد") from None


@router.post("/api/analytics/sample-now")
async def analytics_sample_now(
    _admin=Depends(get_current_admin),
):
    """Force one analytics sample cycle (admin/debug)."""
    stats = await analytics_svc.sample_all_users_once()
    panel_stats = await panel_analytics_svc.sample_all_panels_once(force=True)
    return {"ok": True, "users": stats, "panels": panel_stats}
