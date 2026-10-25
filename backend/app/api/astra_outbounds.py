"""CRUD + ping for Astra panel outbound pool."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin
from app.core.database import get_db
from app.models.panel import Panel, PanelType
from app.schemas.astra_outbound import (
    AstraNodeClientEnable,
    AstraNodeClientOut,
    AstraNodeHealth,
    AstraOutboundBulkCreate,
    AstraOutboundBulkResult,
    AstraOutboundEnable,
    AstraOutboundOut,
)
from app.services import astra_node as node

router = APIRouter(tags=["AstraOutbounds"])


async def _astra_panel(db: AsyncSession, panel_id: int) -> Panel:
    panel = await db.get(Panel, panel_id)
    if not panel:
        raise HTTPException(status_code=404, detail="Panel not found")
    if panel.type != PanelType.astra:
        raise HTTPException(status_code=400, detail="فقط برای پنل نوع Astra")
    return panel


def _out(row) -> AstraOutboundOut:
    return AstraOutboundOut(
        id=row.id,
        panel_id=row.panel_id,
        uri=row.uri,
        remark=row.remark,
        protocol=row.protocol,
        host=row.host,
        port=row.port,
        is_enabled=row.is_enabled,
        last_ping_ms=row.last_ping_ms,
        last_ping_ok=row.last_ping_ok,
        last_ping_at=row.last_ping_at,
        sort_order=row.sort_order,
        created_at=row.created_at,
    )


@router.get(
    "/api/admin/panels/{panel_id}/outbounds",
    response_model=list[AstraOutboundOut],
)
async def list_outbounds(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    await _astra_panel(db, panel_id)
    rows = await node.list_outbounds(db, panel_id)
    return [_out(r) for r in rows]


@router.post(
    "/api/admin/panels/{panel_id}/outbounds",
    response_model=AstraOutboundBulkResult,
)
async def bulk_add_outbounds(
    panel_id: int,
    body: AstraOutboundBulkCreate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    panel = await _astra_panel(db, panel_id)
    created, errors = await node.add_outbounds_from_text(db, panel, body.text)
    await db.commit()
    for r in created:
        await db.refresh(r)
    return AstraOutboundBulkResult(
        created=[_out(r) for r in created],
        errors=errors,
        total_created=len(created),
    )


@router.post(
    "/api/admin/panels/{panel_id}/outbounds/ping",
    response_model=list[AstraOutboundOut],
)
async def ping_all(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    panel = await _astra_panel(db, panel_id)
    await node.reconcile_outbounds_health(
        db, panel, concurrency=4, force_refresh=True
    )
    await db.commit()
    rows = await node.list_outbounds(db, panel_id)
    return [_out(r) for r in rows]


@router.post(
    "/api/admin/panels/{panel_id}/outbounds/{outbound_id}/ping",
    response_model=AstraOutboundOut,
)
async def ping_one(
    panel_id: int,
    outbound_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    from app.models.astra_outbound import AstraOutbound

    panel = await _astra_panel(db, panel_id)
    row = await db.get(AstraOutbound, outbound_id)
    if not row or row.panel_id != panel_id:
        raise HTTPException(status_code=404, detail="Outbound not found")
    await node.ping_outbound(db, row)
    # Recovered outbound must re-enter balancer + subscription share-links
    await node.rebuild_xray_config(db, panel)
    await node.refresh_panel_account_links(db, panel)
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.patch(
    "/api/admin/panels/{panel_id}/outbounds/{outbound_id}",
    response_model=AstraOutboundOut,
)
async def set_enabled(
    panel_id: int,
    outbound_id: int,
    body: AstraOutboundEnable,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    panel = await _astra_panel(db, panel_id)
    row = await node.set_outbound_enabled(db, panel, outbound_id, body.is_enabled)
    if not row:
        raise HTTPException(status_code=404, detail="Outbound not found")
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.delete("/api/admin/panels/{panel_id}/outbounds/{outbound_id}")
async def remove_outbound(
    panel_id: int,
    outbound_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    panel = await _astra_panel(db, panel_id)
    ok = await node.delete_outbound(db, panel, outbound_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Outbound not found")
    await db.commit()
    return {"ok": True}


@router.get(
    "/api/admin/panels/{panel_id}/astra-health",
    response_model=AstraNodeHealth,
)
async def astra_health(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    panel = await _astra_panel(db, panel_id)
    return AstraNodeHealth(**node.node_health(panel))


@router.post("/api/admin/panels/{panel_id}/astra-rebuild")
async def astra_rebuild(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    panel = await _astra_panel(db, panel_id)
    path = await node.rebuild_xray_config(db, panel)
    await db.commit()
    return {"ok": True, "config": str(path) if path else None}


@router.get(
    "/api/admin/panels/{panel_id}/clients",
    response_model=list[AstraNodeClientOut],
)
async def list_astra_clients(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Configs / accounts created on this Astra outbound panel."""
    panel = await _astra_panel(db, panel_id)
    rows = await node.list_clients_enriched(db, panel)
    await db.commit()
    return [AstraNodeClientOut(**r) for r in rows]


@router.patch(
    "/api/admin/panels/{panel_id}/clients/{email}",
    response_model=AstraNodeClientOut,
)
async def set_astra_client_enable(
    panel_id: int,
    email: str,
    body: AstraNodeClientEnable,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    panel = await _astra_panel(db, panel_id)
    row = await node.set_client_enable(db, panel, email, body.enable)
    if not row:
        raise HTTPException(status_code=404, detail="کلاینت پیدا نشد")
    await db.commit()
    enriched = await node.list_clients_enriched(db, panel)
    await db.commit()
    for r in enriched:
        if r.get("email") == email:
            return AstraNodeClientOut(**r)
    return AstraNodeClientOut(
        id=row.id,
        panel_id=row.panel_id,
        email=row.email,
        uuid=row.uuid,
        enable=row.enable,
        data_limit=int(row.data_limit or 0),
        used_traffic=int(row.used_up or 0) + int(row.used_down or 0),
        limit_ip=int(row.limit_ip or 0),
        expire=int(row.expire or 0),
        share_links=[],
        subscription_url=None,
    )


@router.post("/api/admin/panels/{panel_id}/clients/sync-usage")
async def sync_astra_clients_usage(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    panel = await _astra_panel(db, panel_id)
    n = await node.sync_traffic_from_xray(db, panel)
    await db.commit()
    return {"ok": True, "updated": n}
