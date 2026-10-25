import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
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
    PanelActiveSet,
    PanelCreate,
    PanelOut,
    PanelUpdate,
    RemoteGroupOut,
)

router = APIRouter(tags=["Panel"])
logger = logging.getLogger("astra.panels")


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
        out: list[InboundOut] = []
        for row in rows:
            try:
                out.append(InboundOut(**row))
            except ValidationError as ve:
                logger.warning("skip inbound row %s: %s", row, ve)
                continue
        return out
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).strip() or exc.__class__.__name__
        logger.exception("sanaei inbounds failed for %s: %s", panel.base_url, msg)
        raise HTTPException(status_code=400, detail=f"دریافت اینباندها ناموفق: {msg}") from exc
    finally:
        await _close_adapter(adapter)


async def _fetch_pasarguard_groups(panel: Panel) -> list[RemoteGroupOut]:
    if panel.type != PanelType.pasarguard:
        raise HTTPException(status_code=400, detail="فقط برای پنل پاسارگاد لیست گروه در دسترس است")
    adapter = get_adapter(panel)
    try:
        rows = await adapter.list_groups()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).strip() or exc.__class__.__name__
        logger.warning("pasarguard groups failed for %s: %s", panel.base_url, msg)
        raise HTTPException(status_code=400, detail=f"دریافت گروه‌ها ناموفق: {msg}") from exc
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
        msg = str(exc).strip() or exc.__class__.__name__
        logger.warning("marz protocols failed for %s: %s", panel.base_url, msg)
        raise HTTPException(status_code=400, detail=f"دریافت {label} ناموفق: {msg}") from exc
    finally:
        await _close_adapter(adapter)
    out: list[MarzProtocolOut] = []
    for row in rows:
        try:
            out.append(MarzProtocolOut(**row))
        except ValidationError as ve:
            logger.warning("skip marz protocol row %s: %s", row, ve)
            continue
    return out


def _validate_credentials(panel_type: PanelType, username: str | None, password: str | None, api_token: str | None) -> None:
    if panel_type == PanelType.astra:
        # Local node — no remote admin password; base_url = public entry host
        return
    if panel_type == PanelType.sanaei:
        token = (api_token or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        user = (username or "").strip()
        pwd = (password or "").strip()
        if not user or not pwd:
            raise HTTPException(
                status_code=400,
                detail="برای سنایی نام کاربری و رمز عبور ادمین الزامی است",
            )
        if not token:
            raise HTTPException(
                status_code=400,
                detail="برای سنایی (3x-ui) API Token الزامی است — Settings → Security → API Token",
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
    """Live-test remote panel; raise 400 if credentials/URL are wrong.

    Marzban Cloud / distant hosts often flake on the first token — retry with backoff.
    """
    last_err = "اطلاعات ورود یا آدرس اشتباه است"
    # Marzban/Rebecca: more attempts (token + /api/admin can be slow/flaky)
    tries = 4 if panel.type in (PanelType.marzban, PanelType.rebecca) else 2
    for attempt in range(tries):
        adapter = get_adapter(panel)
        result = None
        try:
            # Drop any stale shared token before a fresh probe
            clear = getattr(adapter, "_token", None)
            if clear is not None:
                adapter._token = None  # type: ignore[attr-defined]
            cache = getattr(adapter, "_cache_key", None)
            if callable(cache):
                try:
                    from app.adapters import marzban as marz_mod

                    marz_mod._TOKEN_CACHE.pop(adapter._cache_key(), None)  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
            result = await adapter.test_connection()
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc).strip() or exc.__class__.__name__
            logger.warning(
                "panel probe exception attempt=%s type=%s url=%s: %s",
                attempt + 1,
                panel.type,
                panel.base_url,
                last_err,
            )
            result = None
        finally:
            await _close_adapter(adapter)

        if result is not None and result.success:
            return

        last_err = (
            (result.error if result else None) or last_err or "اطلاعات ورود یا آدرس اشتباه است"
        )
        if attempt + 1 < tries:
            await asyncio.sleep(0.8 + attempt * 0.7)

    logger.warning("panel probe failed type=%s url=%s: %s", panel.type, panel.base_url, last_err)
    raise HTTPException(
        status_code=400,
        detail=f"اتصال ناموفق — پنل ذخیره نشد: {last_err}",
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

    meta = dict(body.meta or {})
    if body.type == PanelType.astra:
        from app.services.astra_node import ensure_panel_token

        meta = ensure_panel_token(meta)
        # Public client host — accept bare host or URL
        base = str(body.base_url or "").strip() or "127.0.0.1"
        if "://" not in base:
            base = f"https://{base}"
        panel = Panel(
            name=body.name,
            type=body.type,
            base_url=normalize_panel_base_url(base),
            username=None,
            password=None,
            api_token=str(meta.get("node_token") or ""),
            meta=meta,
            is_active=body.is_active,
        )
        db.add(panel)
        await db.flush()
        # Persist id into meta for adapter resolution, write initial xray config
        meta = ensure_panel_token(meta, panel_id=panel.id)
        meta["panel_id"] = panel.id
        if not meta.get("public_host"):
            meta["public_host"] = panel.base_url.replace("https://", "").replace(
                "http://", ""
            ).split("/")[0].split(":")[0]
        panel.meta = meta
        from app.services import astra_node as astra_node_svc

        await astra_node_svc.rebuild_xray_config(db, panel)
        panel.last_ok_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(panel)
        return _to_out(panel)

    panel = Panel(
        name=body.name,
        type=body.type,
        base_url=normalize_panel_base_url(str(body.base_url)),
        username=(body.username or "").strip() or None,
        password=(body.password or "").strip() or None,
        api_token=(body.api_token or "").strip() or None,
        meta=meta,
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
        username=(body.username or "").strip(),
        password=(body.password or "").strip(),
        api_token=(body.api_token or "").strip() or None,
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
    only_active = set(data.keys()) == {"is_active"}
    was_active = bool(panel.is_active)
    old_astra_meta = (
        dict(panel.meta or {})
        if panel.type == PanelType.astra and isinstance(panel.meta, dict)
        else {}
    )
    for k, v in data.items():
        if k == "base_url" and v:
            v = normalize_panel_base_url(str(v))
        elif k in ("username", "password", "api_token") and isinstance(v, str):
            v = v.strip() or None
        setattr(panel, k, v)

    if only_active:
        # Toggle on/off must not require a live probe (panel may be temporarily down)
        if was_active and not bool(panel.is_active):
            from app.services.orchestrator import drop_inactive_panel_errors

            await drop_inactive_panel_errors(db, panel.id)
        await db.commit()
        await db.refresh(panel)
        if (not was_active) and bool(panel.is_active):
            asyncio.create_task(
                _bg_heal_panel(panel.id),
                name=f"astra-panel-heal-{panel.id}",
            )
        return _to_out(panel)

    _validate_credentials(panel.type, panel.username, panel.password, panel.api_token)
    if panel.type == PanelType.astra:
        from app.services import astra_node as astra_node_svc
        from app.services.astra_node import ensure_panel_token

        meta = ensure_panel_token(dict(panel.meta or {}), panel_id=panel.id)
        meta["panel_id"] = panel.id
        if body.base_url or meta.get("public_host"):
            host = str(meta.get("public_host") or "").strip()
            if not host:
                raw = (panel.base_url or "").replace("https://", "").replace("http://", "")
                host = raw.split("/")[0].split(":")[0]
                meta["public_host"] = host
            # Keep base_url aligned with public client host
            if host:
                panel.base_url = normalize_panel_base_url(f"https://{host}")
        panel.meta = meta
        panel.api_token = str(meta.get("node_token") or panel.api_token or "")
        await astra_node_svc.rebuild_xray_config(db, panel)
        # Always rewrite share links so subs pick up new host/port/path
        n = await astra_node_svc.refresh_panel_account_links(db, panel)
        logger.info(
            "astra panel=%s link refresh: %s accounts · host %s→%s port %s→%s",
            panel.id,
            n,
            old_astra_meta.get("public_host"),
            meta.get("public_host"),
            old_astra_meta.get("public_port"),
            meta.get("public_port"),
        )
        panel.last_ok_at = datetime.now(timezone.utc)
    else:
        # Re-test with new credentials; reject update if remote rejects us
        await _probe_panel(panel)
        panel.last_ok_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(panel)
    if (not was_active) and bool(panel.is_active):
        asyncio.create_task(
            _bg_heal_panel(panel.id),
            name=f"astra-panel-heal-{panel.id}",
        )
    return _to_out(panel)


@router.put("/api/panel/{panel_id}/active", response_model=PanelOut)
@router.put("/api/admin/panels/{panel_id}/active", response_model=PanelOut)
async def set_panel_active(
    panel_id: int,
    body: PanelActiveSet,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """
    Turn panel on/off without probing remotes.

    Off → excluded from groups provision + all old/new subscriptions immediately.
    On  → background heal provisions missing accounts on linked groups.
    """
    panel = await db.get(Panel, panel_id)
    if not panel:
        raise HTTPException(status_code=404, detail="Panel not found")
    was_active = bool(panel.is_active)
    panel.is_active = bool(body.is_active)
    if was_active and not bool(panel.is_active):
        from app.services.orchestrator import drop_inactive_panel_errors

        await drop_inactive_panel_errors(db, panel.id)
    await db.commit()
    await db.refresh(panel)
    if (not was_active) and bool(panel.is_active):
        asyncio.create_task(
            _bg_heal_panel(panel.id),
            name=f"astra-panel-heal-{panel.id}",
        )
    return _to_out(panel)


async def _bg_heal_panel(panel_id: int) -> None:
    from app.services.orchestrator import heal_panel_after_enable

    try:
        await heal_panel_after_enable(panel_id)
    except Exception:  # noqa: BLE001
        logger.exception("bg heal panel=%s failed", panel_id)


@router.delete("/api/panel/{panel_id}")
@router.delete("/api/admin/panels/{panel_id}")
async def delete_panel(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Remove panel from Astra AFTER deleting its configs on the remote panel.

    Every PanelAccount on this panel is deleted upstream (Marzban / PasarGuard /
    Sanaei / Rebecca) so old and new subs lose those configs and remotes stay clean.
    """
    from sqlalchemy import delete as sa_delete

    from app.models.astra_outbound import AstraNodeClient, AstraOutbound
    from app.models.group import GroupPanel
    from app.services.orchestrator import deprovision_all_accounts_on_panel

    panel = await db.get(Panel, panel_id)
    if not panel:
        raise HTTPException(status_code=404, detail="Panel not found")

    was_astra = panel.type == PanelType.astra
    astra_meta = dict(panel.meta or {}) if was_astra else {}

    stats = await deprovision_all_accounts_on_panel(db, panel_id)

    # Explicit child cleanup — avoids SQLite NOT NULL on FK nulling (async ORM)
    await db.execute(sa_delete(AstraOutbound).where(AstraOutbound.panel_id == panel_id))
    await db.execute(sa_delete(AstraNodeClient).where(AstraNodeClient.panel_id == panel_id))
    await db.execute(sa_delete(GroupPanel).where(GroupPanel.panel_id == panel_id))

    await db.delete(panel)
    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        logger.exception("delete panel=%s failed", panel_id)
        raise HTTPException(status_code=500, detail=f"حذف پنل ناموفق: {exc}") from exc

    if was_astra:
        try:
            from app.services import astra_node as astra_node_svc

            await astra_node_svc.cleanup_panel_node(panel_id, astra_meta)
        except Exception:  # noqa: BLE001
            logger.exception("astra node cleanup panel=%s failed", panel_id)

    return {
        "detail": "Panel deleted",
        "remote_deleted": stats.get("ok", 0),
        "remote_failed": stats.get("fail", 0),
        "accounts": stats.get("accounts", 0),
    }


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
