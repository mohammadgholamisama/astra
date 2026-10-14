from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.factory import get_adapter
from app.adapters.http_utils import normalize_panel_base_url
from app.api.deps import get_current_admin
from app.core.database import get_db
from app.models.panel import Panel, PanelType
from app.schemas.panel import (
    GroupPreviewRequest,
    InboundOut,
    InboundPreviewRequest,
    MarzInboundOut,
    MarzInboundPreviewRequest,
    MarzProtocolOut,
    PanelCreate,
    PanelOut,
    PanelUpdate,
    RemoteGroupOut,
)

router = APIRouter(tags=["Panel"])


async def _close_adapter(adapter) -> None:
    close = getattr(adapter, "close", None)
    if callable(close):
        try:
            await close()
        except Exception:
            pass


async def _fetch_sanaei_inbounds(panel: Panel) -> list[InboundOut]:
    if panel.type != PanelType.sanaei:
        raise HTTPException(status_code=400, detail="فقط برای پنل سنایی لیست اینباند در دسترس است")
    adapter = get_adapter(panel)
    try:
        rows = await adapter.list_inbounds()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"دریافت اینباندها ناموفق: {exc}") from exc
    finally:
        await _close_adapter(adapter)
    return [InboundOut(**row) for row in rows]


async def _fetch_pasarguard_groups(panel: Panel) -> list[RemoteGroupOut]:
    if panel.type != PanelType.pasarguard:
        raise HTTPException(status_code=400, detail="فقط برای پنل پاسارگاد لیست گروه در دسترس است")
    adapter = get_adapter(panel)
    try:
        rows = await adapter.list_groups()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"دریافت گروه‌ها ناموفق: {exc}") from exc
    finally:
        await _close_adapter(adapter)
    return [RemoteGroupOut(**row) for row in rows]


async def _fetch_marz_protocols(panel: Panel) -> list[MarzProtocolOut]:
    if panel.type not in (PanelType.marzban, PanelType.rebecca):
        raise HTTPException(status_code=400, detail="فقط برای مرزبان/ربکا لیست پروتکل/سرویس در دسترس است")
    adapter = get_adapter(panel)
    try:
        rows = await adapter.list_protocols()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        label = "سرویس‌ها" if panel.type == PanelType.rebecca else "پروتکل‌ها"
        raise HTTPException(status_code=400, detail=f"دریافت {label} ناموفق: {exc}") from exc
    finally:
        await _close_adapter(adapter)
    return [MarzProtocolOut(**row) for row in rows]


def _validate_credentials(panel_type: PanelType, username: str | None, password: str | None, api_token: str | None) -> None:
    if panel_type == PanelType.sanaei:
        if not api_token:
            raise HTTPException(
                status_code=400,
                detail="برای سنایی (3x-ui) فیلد API Token الزامی است (Settings → Security → API Token)",
            )
        if not username or not password:
            raise HTTPException(
                status_code=400,
                detail="برای سنایی نام کاربری و رمز عبور پنل هم لازم است",
            )
    else:
        # marzban / rebecca / pasarguard
        if not username or not password:
            raise HTTPException(
                status_code=400,
                detail="برای این پنل نام کاربری و رمز عبور ادمین الزامی است",
            )


def _to_out(p: Panel) -> PanelOut:
    return PanelOut(
        id=p.id,
        name=p.name,
        type=p.type,
        base_url=p.base_url,
        username=p.username,
        has_password=bool(p.password),
        has_token=bool(p.api_token),
        meta=p.meta or {},
        is_active=p.is_active,
        last_ok_at=p.last_ok_at,
        created_at=p.created_at,
    )


async def _probe_panel(panel: Panel) -> None:
    """Live-test remote panel; raise 400 if credentials/URL are wrong."""
    adapter = get_adapter(panel)
    result = None
    try:
        result = await adapter.test_connection()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"اتصال برقرار نشد: {exc}") from exc
    finally:
        close = getattr(adapter, "close", None)
        if callable(close):
            try:
                await close()
            except Exception:
                pass
    if result is None or not result.success:
        raise HTTPException(
            status_code=400,
            detail=(
                "اتصال ناموفق — پنل ذخیره نشد: "
                f"{(result.error if result else None) or 'اطلاعات ورود یا آدرس اشتباه است'}"
            ),
        )


@router.post("/api/panel", response_model=PanelOut)
@router.post("/api/admin/panels", response_model=PanelOut)
async def create_panel(
    body: PanelCreate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    existing = await db.execute(select(Panel).where(Panel.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Panel name already exists")
    _validate_credentials(body.type, body.username, body.password, body.api_token)

    panel = Panel(
        name=body.name,
        type=body.type,
        base_url=normalize_panel_base_url(str(body.base_url)),
        username=body.username,
        password=body.password,
        api_token=body.api_token,
        meta=body.meta or {},
        is_active=body.is_active,
    )
    # Test BEFORE persisting — wrong credentials must not create a row
    await _probe_panel(panel)
    panel.last_ok_at = datetime.now(timezone.utc)

    db.add(panel)
    await db.commit()
    await db.refresh(panel)
    return _to_out(panel)


@router.get("/api/panel", response_model=list[PanelOut])
@router.get("/api/admin/panels", response_model=list[PanelOut])
async def list_panels(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    result = await db.execute(select(Panel).order_by(Panel.id))
    return [_to_out(p) for p in result.scalars().all()]


@router.post("/api/admin/panels/preview-inbounds", response_model=list[InboundOut])
@router.post("/api/panel/preview-inbounds", response_model=list[InboundOut])
async def preview_inbounds(
    body: InboundPreviewRequest,
    _admin=Depends(get_current_admin),
):
    """List Sanaei inbounds using credentials before the panel is saved."""
    panel = Panel(
        name="_preview",
        type=PanelType.sanaei,
        base_url=normalize_panel_base_url(str(body.base_url)),
        username=body.username,
        password=body.password,
        api_token=body.api_token,
        meta={},
        is_active=True,
    )
    return await _fetch_sanaei_inbounds(panel)


@router.post("/api/admin/panels/preview-groups", response_model=list[RemoteGroupOut])
@router.post("/api/panel/preview-groups", response_model=list[RemoteGroupOut])
async def preview_groups(
    body: GroupPreviewRequest,
    _admin=Depends(get_current_admin),
):
    """List PasarGuard groups using credentials before the panel is saved."""
    panel = Panel(
        name="_preview_pg",
        type=PanelType.pasarguard,
        base_url=normalize_panel_base_url(str(body.base_url)),
        username=body.username,
        password=body.password,
        api_token=body.api_token,
        meta={},
        is_active=True,
    )
    return await _fetch_pasarguard_groups(panel)


@router.post("/api/admin/panels/preview-marz-protocols", response_model=list[MarzProtocolOut])
@router.post("/api/panel/preview-marz-protocols", response_model=list[MarzProtocolOut])
@router.post("/api/admin/panels/preview-marz-inbounds", response_model=list[MarzProtocolOut])
@router.post("/api/panel/preview-marz-inbounds", response_model=list[MarzProtocolOut])
async def preview_marz_protocols(
    body: MarzInboundPreviewRequest,
    _admin=Depends(get_current_admin),
):
    """List Marzban/Rebecca protocols (from /api/inbounds) before the panel is saved."""
    if body.panel_type not in (PanelType.marzban, PanelType.rebecca):
        raise HTTPException(status_code=400, detail="panel_type باید marzban یا rebecca باشد")
    panel = Panel(
        name="_preview_marz",
        type=body.panel_type,
        base_url=normalize_panel_base_url(str(body.base_url)),
        username=body.username,
        password=body.password,
        api_token=None,
        meta={},
        is_active=True,
    )
    return await _fetch_marz_protocols(panel)


@router.get("/api/panel/{panel_id}", response_model=PanelOut)
@router.get("/api/admin/panels/{panel_id}", response_model=PanelOut)
async def get_panel(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    panel = await db.get(Panel, panel_id)
    if not panel:
        raise HTTPException(status_code=404, detail="Panel not found")
    return _to_out(panel)


@router.put("/api/panel/{panel_id}", response_model=PanelOut)
@router.put("/api/admin/panels/{panel_id}", response_model=PanelOut)
async def update_panel(
    panel_id: int,
    body: PanelUpdate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    panel = await db.get(Panel, panel_id)
    if not panel:
        raise HTTPException(status_code=404, detail="Panel not found")
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        if k == "base_url" and v:
            v = normalize_panel_base_url(str(v))
        setattr(panel, k, v)

    _validate_credentials(panel.type, panel.username, panel.password, panel.api_token)
    # Re-test with new credentials; reject update if remote rejects us
    await _probe_panel(panel)
    panel.last_ok_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(panel)
    return _to_out(panel)


@router.delete("/api/panel/{panel_id}")
@router.delete("/api/admin/panels/{panel_id}")
async def delete_panel(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    panel = await db.get(Panel, panel_id)
    if not panel:
        raise HTTPException(status_code=404, detail="Panel not found")
    await db.delete(panel)
    await db.commit()
    return {"detail": "Panel deleted"}


@router.post("/api/panel/{panel_id}/test")
@router.post("/api/admin/panels/{panel_id}/test")
async def test_panel(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    panel = await db.get(Panel, panel_id)
    if not panel:
        raise HTTPException(status_code=404, detail="Panel not found")
    adapter = get_adapter(panel)
    result = await adapter.test_connection()
    if result.success:
        panel.last_ok_at = datetime.now(timezone.utc)
        await db.commit()
    return {"success": result.success, "error": result.error, "raw": result.raw}


@router.get("/api/admin/panels/{panel_id}/inbounds", response_model=list[InboundOut])
@router.get("/api/panel/{panel_id}/inbounds", response_model=list[InboundOut])
async def list_panel_inbounds(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    panel = await db.get(Panel, panel_id)
    if not panel:
        raise HTTPException(status_code=404, detail="Panel not found")
    return await _fetch_sanaei_inbounds(panel)


@router.get("/api/admin/panels/{panel_id}/groups", response_model=list[RemoteGroupOut])
@router.get("/api/panel/{panel_id}/groups", response_model=list[RemoteGroupOut])
async def list_panel_groups(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    panel = await db.get(Panel, panel_id)
    if not panel:
        raise HTTPException(status_code=404, detail="Panel not found")
    return await _fetch_pasarguard_groups(panel)


@router.get("/api/admin/panels/{panel_id}/marz-protocols", response_model=list[MarzProtocolOut])
@router.get("/api/panel/{panel_id}/marz-protocols", response_model=list[MarzProtocolOut])
@router.get("/api/admin/panels/{panel_id}/marz-inbounds", response_model=list[MarzProtocolOut])
@router.get("/api/panel/{panel_id}/marz-inbounds", response_model=list[MarzProtocolOut])
async def list_panel_marz_protocols(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    panel = await db.get(Panel, panel_id)
    if not panel:
        raise HTTPException(status_code=404, detail="Panel not found")
    return await _fetch_marz_protocols(panel)
