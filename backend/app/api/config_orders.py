from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin
from app.core.database import get_db
from app.schemas.config_order import (
    ConfigOrderSaveBody,
    ConfigOrderWorkspaceOut,
)
from app.services import config_orders as order_svc

router = APIRouter(tags=["Config Order"])


@router.get("/api/admin/config-order", response_model=ConfigOrderWorkspaceOut)
async def get_config_order(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    return await order_svc.list_workspace(db)


@router.put("/api/admin/config-order", response_model=ConfigOrderWorkspaceOut)
async def put_config_order(
    body: ConfigOrderSaveBody,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    try:
        return await order_svc.save_order(db, body.items)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/config-order/sample")
async def refresh_config_order_samples(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Wipe previous order probe on remotes, recreate on every active panel."""
    try:
        stats = await order_svc.create_or_refresh_samples(db)
        workspace = await order_svc.list_workspace(db)
        return {
            "ok": True,
            "sample": stats,
            "workspace": workspace.model_dump(),
        }
    except ValueError as exc:
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(
            status_code=500,
            detail=f"خطا هنگام ساخت نمونه روی همه پنل‌ها: {exc}",
        ) from exc
