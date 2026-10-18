from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin
from app.core.database import get_db
from app.schemas.config_hide import (
    ConfigVisibilityAllOut,
    ConfigVisibilityOut,
    ConfigVisibilitySet,
    SampleAllOut,
)
from app.schemas.group_config import GroupConfigBulkUpdate, GroupConfigWorkspaceOut
from app.services import config_hides as hide_svc
from app.services import group_configs as svc

router = APIRouter(tags=["GroupConfigs"])


@router.get("/api/group/{group_id}/configs", response_model=GroupConfigWorkspaceOut)
async def get_group_configs(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    try:
        return await svc.get_workspace(db, group_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/group/{group_id}/configs/sample", response_model=GroupConfigWorkspaceOut)
async def refresh_group_sample(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Create/refresh a sample user on all panels of the group and list configs."""
    try:
        return await svc.create_or_refresh_sample(db, group_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        # Always surface the real error — never pretend success with an empty list
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(
            status_code=500,
            detail=f"خطا هنگام ساخت نمونه روی پنل‌ها: {exc}",
        ) from exc


@router.delete("/api/group/{group_id}/configs/sample", response_model=GroupConfigWorkspaceOut)
async def delete_group_sample(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """
    Delete the probe/test user from remotes. Hide rules (host/port/match_host)
    and discovered config rows stay so visibility settings remain.
    """
    try:
        return await svc.delete_sample(db, group_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(
            status_code=500,
            detail=f"خطا هنگام حذف نمونه از پنل‌ها: {exc}",
        ) from exc


@router.put("/api/group/{group_id}/configs", response_model=GroupConfigWorkspaceOut)
async def update_group_configs(
    group_id: int,
    body: GroupConfigBulkUpdate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """
    Save coefficient (for future Astra subscription) and IP limits.
    IP limits are written to GroupPanel.config and pushed to the sample user on remotes.
    """
    try:
        return await svc.update_workspace_items(db, group_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/api/admin/config-visibility",
    response_model=ConfigVisibilityAllOut,
)
async def get_config_visibility_all(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """List configs from every panel across all active sales groups (DB only)."""
    data = await hide_svc.build_visibility_all(db)
    return ConfigVisibilityAllOut(**data)


@router.post("/api/admin/configs/sample", response_model=SampleAllOut)
async def refresh_all_samples(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Create/refresh probe users on every active group (sequential)."""
    try:
        data = await svc.create_or_refresh_sample_all(db)
        return SampleAllOut(**data)
    except Exception as exc:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(
            status_code=500,
            detail=f"خطا هنگام ساخت نمونه روی همه گروه‌ها: {exc}",
        ) from exc


@router.delete("/api/admin/configs/sample", response_model=SampleAllOut)
async def delete_all_samples(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Delete probe users from every active group; hide rules stay."""
    try:
        data = await svc.delete_sample_all(db)
        return SampleAllOut(**data)
    except Exception as exc:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(
            status_code=500,
            detail=f"خطا هنگام حذف نمونه‌ها از پنل‌ها: {exc}",
        ) from exc


@router.put("/api/admin/config-visibility")
async def set_config_visibility_global(
    body: ConfigVisibilitySet,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Globally hide/show host:port[+match_host] from ALL subscription feeds.

    Returns a lightweight ack — Config Order page only needs success + hidden flag.
    Avoid rebuilding the full visibility list (slow / lock-prone under load).
    """
    try:
        await hide_svc.set_hidden(
            db,
            None,
            body.host,
            body.port,
            hidden=body.hidden,
            match_host=body.match_host or "",
        )
        return {
            "ok": True,
            "host": body.host,
            "port": int(body.port),
            "match_host": body.match_host or "",
            "hidden": bool(body.hidden),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(
            status_code=500,
            detail=f"خطا در تغییر نمایش کانفیگ: {exc}",
        ) from exc


@router.get(
    "/api/group/{group_id}/config-visibility",
    response_model=ConfigVisibilityOut,
)
async def get_config_visibility(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """List unique host:port configs for a group + global hide flags."""
    try:
        data = await hide_svc.build_visibility(db, group_id)
        return ConfigVisibilityOut(**data)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put(
    "/api/group/{group_id}/config-visibility",
    response_model=ConfigVisibilityOut,
)
async def set_config_visibility(
    group_id: int,
    body: ConfigVisibilitySet,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Hide/show endpoint globally (group_id is audit context only)."""
    try:
        await hide_svc.set_hidden(
            db,
            group_id,
            body.host,
            body.port,
            hidden=body.hidden,
            match_host=body.match_host or "",
        )
        data = await hide_svc.build_visibility(db, group_id)
        return ConfigVisibilityOut(**data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
