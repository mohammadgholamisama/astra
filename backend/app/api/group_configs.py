from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin
from app.core.database import get_db
from app.schemas.group_config import GroupConfigBulkUpdate, GroupConfigWorkspaceOut
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
        # Prefer returning workspace with per-panel errors over a bare 500
        try:
            await db.rollback()
            ws = await svc.get_workspace(db, group_id)
            # attach failure hint via raising only if workspace itself is empty/useless
            if ws.accounts or ws.items:
                return ws
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(
            status_code=500,
            detail=f"خطا هنگام ساخت نمونه روی پنل‌ها: {exc}",
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
