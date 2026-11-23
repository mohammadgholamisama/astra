import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.factory import get_adapter
from app.adapters.http_utils import normalize_panel_base_url
from app.api.deps import get_current_admin
from app.core.database import get_db
from app.models.panel import Panel, PanelCategory, PanelType
from app.schemas.panel import (
    GroupPreviewRequest,
    InboundOut,
    InboundPreviewRequest,
    MarzInboundOut,
    MarzInboundPreviewRequest,
    MarzProtocolOut,
    PanelActiveSet,
    PanelCategoryCreate,
    PanelCategoryOut,
    PanelCategoryUpdate,
    PanelCreate,
    PanelMoveBody,
    PanelOut,
    PanelReorderBody,
    PanelTestAllOut,
    PanelTestAllStats,
    PanelTestOut,
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


def _clear_adapter_auth_cache(adapter) -> None:
    """Drop stale JWT so a live test doesn't reuse a dead token."""
    cached = getattr(adapter, "_token", None)
    if cached is not None and not callable(cached):
        try:
            adapter._token = None  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    cache_key = getattr(adapter, "_cache_key", None)
    if callable(cache_key):
        try:
            from app.adapters import marzban as marz_mod

            marz_mod._TOKEN_CACHE.pop(cache_key(), None)
        except Exception:  # noqa: BLE001
            pass


_HARD_AUTH_MARKERS = (
    "401",
    "403",
    "unauthorized",
    "forbidden",
    "رمز",
    "ورود",
    "credential",
    "invalid token",
    "authentication",
)


def _is_hard_auth_fail(err: str | None) -> bool:
    if not err:
        return False
    low = err.lower()
    return any(m in low for m in _HARD_AUTH_MARKERS)


async def _live_test_panel(
    panel: Panel,
    *,
    overall_timeout: float = 18.0,
) -> dict:
    """Accurate connectivity probe for the admin Test button / test-all.

    Clears cached tokens, retries soft failures, and gives Marzban-family
    enough wall time (login + /api/admin can exceed 10s behind CF).
    """
    import time as _time

    from app.adapters.base import AdapterResult

    t0 = _time.monotonic()
    last_err = "اتصال ناموفق"
    # Always allow at least 2 tries for remote panels (token refresh / CF blip).
    tries = 3 if panel.type in (
        PanelType.marzban,
        PanelType.rebecca,
        PanelType.guard,
        PanelType.pasarguard,
        PanelType.sanaei,
    ) else 2

    for attempt in range(tries):
        remaining = overall_timeout - (_time.monotonic() - t0)
        if remaining < 1.0:
            break
        # First try gets most of the budget; later tries use what's left.
        if attempt == 0:
            per_try = min(14.0, remaining)
        else:
            per_try = min(10.0, remaining)
        adapter = get_adapter(panel)
        result: AdapterResult | None = None
        try:
            _clear_adapter_auth_cache(adapter)
            result = await asyncio.wait_for(
                adapter.test_connection(), timeout=per_try
            )
        except asyncio.TimeoutError:
            last_err = (
                "پاسخ پنل از سرور Astra طول کشید "
                "(ممکن است از اینترنت شما باز شود ولی از این سرور دیر برسد)"
            )
            result = None
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc).strip() or exc.__class__.__name__
            result = None
        finally:
            await _close_adapter(adapter)

        if result is not None and result.success:
            ms = int((_time.monotonic() - t0) * 1000)
            return {
                "success": True,
                "error": None,
                "latency_ms": ms,
                "raw": result.raw if isinstance(result.raw, dict) else {},
            }

        last_err = (
            (result.error if result else None) or last_err or "اتصال ناموفق"
        ).strip()
        if _is_hard_auth_fail(last_err):
            break
        if attempt + 1 < tries:
            await asyncio.sleep(0.35 + attempt * 0.25)

    ms = int((_time.monotonic() - t0) * 1000)
    # Clarify path: browser OK ≠ Astra server can reach the panel
    low = last_err.lower()
    if any(
        x in low
        for x in (
            "timeout",
            "timed out",
            "connect",
            "unreachable",
            "name or service",
            "ssl",
            "certificate",
            "طول کشید",
            "connection reset",
            "network",
        )
    ) and "رمز" not in last_err and "password" not in low and "401" not in low:
        last_err = (
            f"{last_err} — از اینترنت شما ممکن است باز شود؛ "
            "تست از مسیر سرور Astra است."
        )
    return {
        "success": False,
        "error": last_err,
        "latency_ms": ms,
        "raw": {},
    }


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
    if panel.type not in (PanelType.marzban, PanelType.rebecca, PanelType.guard):
        raise HTTPException(
            status_code=400,
            detail="فقط برای مرزبان/ربکا/گارد لیست پروتکل/سرویس در دسترس است",
        )
    adapter = get_adapter(panel)
    try:
        rows = await adapter.list_protocols()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        label = (
            "سرویس‌ها"
            if panel.type in (PanelType.rebecca, PanelType.guard)
            else "پروتکل‌ها"
        )
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
    elif panel_type == PanelType.guard:
        token = (api_token or "").strip()
        user = (username or "").strip()
        pwd = (password or "").strip()
        if not token and (not user or not pwd):
            raise HTTPException(
                status_code=400,
                detail="برای گارد یوزر/پسورد ادمین یا API Key الزامی است",
            )
    else:
        # marzban / rebecca / pasarguard
        if not username or not password:
            raise HTTPException(
                status_code=400,
                detail="برای این پنل نام کاربری و رمز عبور ادمین الزامی است",
            )


def _normalize_category_name(raw: str | None) -> str:
    return " ".join(str(raw or "").split()).strip()[:48]


async def _ensure_panel_category(
    db: AsyncSession, category_id: int | None
) -> int | None:
    if category_id is None:
        return None
    cat = await db.get(PanelCategory, int(category_id))
    if not cat:
        raise HTTPException(status_code=400, detail="دسته پنل پیدا نشد")
    return int(cat.id)


async def _release_db_txn(db: AsyncSession) -> None:
    """End idle-in-transaction before slow remote/OS work.

    Holding a Postgres checkout across `_probe_panel` / nginx reload lets
    idle_in_transaction killers close the socket → PendingRollback → false
    «دیتابیس شلوغ است» on save.
    """
    try:
        await db.rollback()
    except Exception:  # noqa: BLE001
        pass


def _is_db_conn_dead(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return (
        "pendingrollback" in msg
        or "connection is closed" in msg
        or "underlying connection is closed" in msg
        or "connectiondoesnotexist" in msg.replace(" ", "")
    )


async def _commit_panel_db(db: AsyncSession, *, label: str) -> None:
    """Commit with one reconnect-style retry after a killed checkout."""
    last: BaseException | None = None
    for attempt in range(3):
        try:
            await db.commit()
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            if not _is_db_conn_dead(exc) or attempt >= 2:
                raise
            logger.warning(
                "panel db commit retry attempt=%s label=%s: %s",
                attempt + 1,
                label,
                exc,
            )
            await _release_db_txn(db)
            await asyncio.sleep(0.15 * (attempt + 1))
    if last:
        raise last


async def _next_panel_sort_order(
    db: AsyncSession, category_id: int | None
) -> int:
    q = select(func.coalesce(func.max(Panel.sort_order), 0))
    if category_id is None:
        q = q.where(Panel.category_id.is_(None))
    else:
        q = q.where(Panel.category_id == int(category_id))
    raw = (await db.execute(q)).scalar()
    try:
        return int(raw or 0) + 10
    except (TypeError, ValueError):
        return 10


def _to_out(p: Panel) -> PanelOut:
    try:
        sort_order = int(getattr(p, "sort_order", 0) or 0)
    except (TypeError, ValueError):
        sort_order = 0
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
        category_id=getattr(p, "category_id", None),
        sort_order=sort_order,
        last_ok_at=p.last_ok_at,
        created_at=p.created_at,
    )


async def _probe_panel(panel: Panel) -> None:
    """Live-test remote panel; raise 400 if credentials/URL are wrong.

    Marzban Cloud / distant hosts often flake on the first token — retry with backoff.
    """
    last_err = "اطلاعات ورود یا آدرس اشتباه است"
    # Marzban/Rebecca: more attempts (token + /api/admin can be slow/flaky)
    tries = 4 if panel.type in (PanelType.marzban, PanelType.rebecca, PanelType.guard) else 2
    for attempt in range(tries):
        adapter = get_adapter(panel)
        result = None
        try:
            # Drop stale Marzban-family JWT cache. Sanaei uses `_token()` as a
            # method — never overwrite that with None (TypeError: not callable).
            cached = getattr(adapter, "_token", None)
            if cached is not None and not callable(cached):
                adapter._token = None  # type: ignore[attr-defined]
            cache_key = getattr(adapter, "_cache_key", None)
            if callable(cache_key):
                try:
                    from app.adapters import marzban as marz_mod

                    marz_mod._TOKEN_CACHE.pop(cache_key(), None)
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


# ── Panel categories (UI folders only) ───────────────────────────────────────


@router.get("/api/panel-categories", response_model=list[PanelCategoryOut])
@router.get("/api/admin/panel-categories", response_model=list[PanelCategoryOut])
async def list_panel_categories(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    counts = {
        int(cid): int(n)
        for cid, n in (
            await db.execute(
                select(Panel.category_id, func.count())
                .where(Panel.category_id.is_not(None))
                .group_by(Panel.category_id)
            )
        ).all()
        if cid is not None
    }
    rows = (
        await db.execute(
            select(PanelCategory).order_by(PanelCategory.sort_order, PanelCategory.id)
        )
    ).scalars().all()
    return [
        PanelCategoryOut(
            id=c.id,
            name=c.name,
            sort_order=c.sort_order,
            panels_count=counts.get(c.id, 0),
            created_at=c.created_at,
        )
        for c in rows
    ]


@router.post("/api/panel-categories", response_model=PanelCategoryOut)
@router.post("/api/admin/panel-categories", response_model=PanelCategoryOut)
async def create_panel_category(
    body: PanelCategoryCreate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    name = _normalize_category_name(body.name)
    if len(name) < 1:
        raise HTTPException(status_code=400, detail="نام دسته خالی است")
    exists = await db.execute(select(PanelCategory).where(PanelCategory.name == name))
    if exists.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="دسته با این نام از قبل هست")
    max_ord = (await db.execute(select(func.max(PanelCategory.sort_order)))).scalar()
    cat = PanelCategory(
        name=name,
        sort_order=body.sort_order if body.sort_order else int(max_ord or 0) + 1,
    )
    db.add(cat)
    await db.commit()
    await db.refresh(cat)
    return PanelCategoryOut(
        id=cat.id,
        name=cat.name,
        sort_order=cat.sort_order,
        panels_count=0,
        created_at=cat.created_at,
    )


@router.put("/api/panel-categories/{category_id}", response_model=PanelCategoryOut)
@router.put("/api/admin/panel-categories/{category_id}", response_model=PanelCategoryOut)
async def update_panel_category(
    category_id: int,
    body: PanelCategoryUpdate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    cat = await db.get(PanelCategory, category_id)
    if not cat:
        raise HTTPException(status_code=404, detail="دسته پیدا نشد")
    if body.name is not None:
        name = _normalize_category_name(body.name)
        if len(name) < 1:
            raise HTTPException(status_code=400, detail="نام دسته خالی است")
        clash = await db.execute(
            select(PanelCategory).where(
                PanelCategory.name == name, PanelCategory.id != category_id
            )
        )
        if clash.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="دسته با این نام از قبل هست")
        cat.name = name
    if body.sort_order is not None:
        cat.sort_order = int(body.sort_order)
    await db.commit()
    await db.refresh(cat)
    n = (
        await db.execute(
            select(func.count()).select_from(Panel).where(Panel.category_id == cat.id)
        )
    ).scalar() or 0
    return PanelCategoryOut(
        id=cat.id,
        name=cat.name,
        sort_order=cat.sort_order,
        panels_count=int(n),
        created_at=cat.created_at,
    )


@router.delete("/api/panel-categories/{category_id}")
@router.delete("/api/admin/panel-categories/{category_id}")
async def delete_panel_category(
    category_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Delete category tab only — panels move to default «همه پنل‌ها»."""
    cat = await db.get(PanelCategory, category_id)
    if not cat:
        raise HTTPException(status_code=404, detail="دسته پیدا نشد")
    moved = await db.execute(
        update(Panel)
        .where(Panel.category_id == category_id)
        .values(category_id=None)
        .execution_options(synchronize_session="fetch")
    )
    await db.flush()
    await db.delete(cat)
    await db.commit()
    return {
        "detail": "Category deleted",
        "panels_moved_to_default": int(moved.rowcount or 0),
    }


@router.put("/api/panel/{panel_id}/move", response_model=PanelOut)
@router.put("/api/admin/panels/{panel_id}/move", response_model=PanelOut)
async def move_panel(
    panel_id: int,
    body: PanelMoveBody,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """UI-only: move panel between category tabs. No provision/sub side effects."""
    panel = await db.get(Panel, panel_id)
    if not panel:
        raise HTTPException(status_code=404, detail="Panel not found")
    new_cat = await _ensure_panel_category(db, body.category_id)
    old_cat = getattr(panel, "category_id", None)
    panel.category_id = new_cat
    if new_cat != old_cat:
        panel.sort_order = await _next_panel_sort_order(db, new_cat)
    await db.commit()
    await db.refresh(panel)
    return _to_out(panel)


@router.post("/api/panel", response_model=PanelOut)
@router.post("/api/admin/panels", response_model=PanelOut)
async def create_panel(
    body: PanelCreate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    try:
        existing = await db.execute(select(Panel).where(Panel.name == body.name))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Panel name already exists")
        _validate_credentials(body.type, body.username, body.password, body.api_token)
        category_id = await _ensure_panel_category(db, body.category_id)

        meta = dict(body.meta or {})
        if body.type == PanelType.astra:
            from app.services.astra_node import ensure_panel_token

            meta = ensure_panel_token(meta)
            # Public client host — accept bare host or URL
            base = str(body.base_url or "").strip() or "127.0.0.1"
            if "://" not in base:
                base = f"https://{base}"
            sort_order = await _next_panel_sort_order(db, category_id)
            panel = Panel(
                name=body.name,
                type=body.type,
                base_url=normalize_panel_base_url(base),
                username=None,
                password=None,
                api_token=str(meta.get("node_token") or ""),
                meta=meta,
                is_active=body.is_active,
                category_id=category_id,
                sort_order=sort_order,
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

            try:
                await astra_node_svc.rebuild_xray_config(db, panel)
            except Exception as exc:  # noqa: BLE001
                logger.exception("astra create rebuild failed name=%s", body.name)
                await db.rollback()
                raise HTTPException(
                    status_code=400,
                    detail=f"ساخت پنل آسترا ناموفق بود: {exc}",
                ) from exc
            panel.last_ok_at = datetime.now(timezone.utc)
            await _commit_panel_db(db, label="create_astra")
            await db.refresh(panel)
            return _to_out(panel)

        sort_order = await _next_panel_sort_order(db, category_id)
        # Detached probe object — must not hold Postgres txn across remote HTTP
        probe = Panel(
            name=body.name,
            type=body.type,
            base_url=normalize_panel_base_url(str(body.base_url)),
            username=(body.username or "").strip() or None,
            password=(body.password or "").strip() or None,
            api_token=(body.api_token or "").strip() or None,
            meta=meta,
            is_active=body.is_active,
            category_id=category_id,
            sort_order=sort_order,
        )
        await _release_db_txn(db)
        # Test BEFORE persisting — wrong credentials must not create a row
        await _probe_panel(probe)

        # Fresh write txn after probe (name may race; sort_order may advance)
        existing = await db.execute(select(Panel).where(Panel.name == body.name))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Panel name already exists")
        category_id = await _ensure_panel_category(db, body.category_id)
        sort_order = await _next_panel_sort_order(db, category_id)
        panel = Panel(
            name=probe.name,
            type=probe.type,
            base_url=probe.base_url,
            username=probe.username,
            password=probe.password,
            api_token=probe.api_token,
            meta=dict(probe.meta or {}),
            is_active=probe.is_active,
            category_id=category_id,
            sort_order=sort_order,
            last_ok_at=datetime.now(timezone.utc),
        )
        db.add(panel)
        await _commit_panel_db(db, label="create_panel")
        await db.refresh(panel)
        return _to_out(panel)
    except HTTPException:
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("create_panel failed name=%s", getattr(body, "name", "?"))
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        msg = str(exc).strip() or exc.__class__.__name__
        # Surface real cause instead of opaque CF/edge «Internal Server Error»
        if "UniqueViolation" in msg or "unique" in msg.lower() or "duplicate" in msg.lower():
            raise HTTPException(
                status_code=409, detail="نام پنل تکراری است"
            ) from exc
        if _is_db_conn_dead(exc):
            raise HTTPException(
                status_code=503,
                detail="ذخیره پنل قطع شد — دوباره ذخیره کنید (اتصال دیتابیس ریست شد)",
            ) from exc
        raise HTTPException(
            status_code=400,
            detail=f"ذخیره پنل ناموفق بود: {msg[:220]}",
        ) from exc


@router.get("/api/panel", response_model=list[PanelOut])
@router.get("/api/admin/panels", response_model=list[PanelOut])
async def list_panels(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    result = await db.execute(
        select(Panel).order_by(Panel.sort_order.asc(), Panel.id.asc())
    )
    return [_to_out(p) for p in result.scalars().all()]


@router.put("/api/panel/reorder", response_model=list[PanelOut])
@router.put("/api/admin/panels/reorder", response_model=list[PanelOut])
async def reorder_panels(
    body: PanelReorderBody,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Persist drag order for one Panels-page tab (UI only)."""
    raw_ids = [int(x) for x in body.panel_ids if x is not None]
    if not raw_ids:
        raise HTTPException(status_code=400, detail="لیست پنل خالی است")

    cat_id = await _ensure_panel_category(db, body.category_id)
    if cat_id is None:
        scope = await db.execute(select(Panel).where(Panel.category_id.is_(None)))
    else:
        scope = await db.execute(
            select(Panel).where(Panel.category_id == int(cat_id))
        )
    scoped = {int(p.id): p for p in scope.scalars().all()}
    if not scoped:
        raise HTTPException(status_code=400, detail="در این تب پنلی نیست")

    seen: set[int] = set()
    ordered: list[int] = []
    for pid in raw_ids:
        if pid in seen or pid not in scoped:
            continue
        seen.add(pid)
        ordered.append(pid)
    # Keep any tab panels missing from the payload at the end (stable prior order)
    missing = sorted(
        (pid for pid in scoped.keys() if pid not in seen),
        key=lambda pid: (
            int(getattr(scoped[pid], "sort_order", 0) or 0),
            pid,
        ),
    )
    final = ordered + missing
    for i, pid in enumerate(final):
        scoped[pid].sort_order = (i + 1) * 10
    await db.commit()
    rows = await db.execute(
        select(Panel)
        .where(Panel.id.in_(final))
        .order_by(Panel.sort_order.asc(), Panel.id.asc())
    )
    return [_to_out(p) for p in rows.scalars().all()]


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
    if body.panel_type not in (PanelType.marzban, PanelType.rebecca, PanelType.guard):
        raise HTTPException(
            status_code=400, detail="panel_type باید marzban یا rebecca یا guard باشد"
        )
    panel = Panel(
        name="_preview_marz",
        type=body.panel_type,
        base_url=normalize_panel_base_url(str(body.base_url)),
        username=body.username,
        password=body.password,
        api_token=(getattr(body, "api_token", None) or None),
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
    try:
        return await _update_panel_inner(panel_id, body, db)
    except HTTPException:
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("update_panel failed id=%s", panel_id)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        msg = str(exc).strip() or exc.__class__.__name__
        if _is_db_conn_dead(exc):
            raise HTTPException(
                status_code=503,
                detail="به‌روزرسانی قطع شد — دوباره ذخیره کنید (اتصال دیتابیس ریست شد)",
            ) from exc
        raise HTTPException(
            status_code=400,
            detail=f"به‌روزرسانی پنل ناموفق بود: {msg[:220]}",
        ) from exc


async def _update_panel_inner(
    panel_id: int,
    body: PanelUpdate,
    db: AsyncSession,
) -> PanelOut:
    panel = await db.get(Panel, panel_id)
    if not panel:
        raise HTTPException(status_code=404, detail="Panel not found")
    data = body.model_dump(exclude_unset=True)
    clear_category = bool(data.pop("clear_category", False))
    old_cat = getattr(panel, "category_id", None)
    if clear_category:
        data["category_id"] = None
    elif "category_id" in data:
        data["category_id"] = await _ensure_panel_category(db, data.get("category_id"))
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
    new_cat = getattr(panel, "category_id", None)
    if "category_id" in data and new_cat != old_cat:
        panel.sort_order = await _next_panel_sort_order(db, new_cat)

    turning_off = was_active and not bool(panel.is_active)
    turning_on = (not was_active) and bool(panel.is_active)

    if only_active:
        # Toggle on/off must not require a live probe (panel may be temporarily down)
        if turning_off:
            await _prepare_panel_turn_off(db, panel.id)
        await _commit_panel_db(db, label="panel_active_toggle")
        await db.refresh(panel)
        if turning_off:
            _schedule_panel_freeze(panel.id)
        if turning_on:
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
        # Snapshot credentials, release txn, probe, then re-apply + commit
        probe = Panel(
            name=panel.name,
            type=panel.type,
            base_url=panel.base_url,
            username=panel.username,
            password=panel.password,
            api_token=panel.api_token,
            meta=dict(panel.meta or {}),
            is_active=True,
        )
        pending = dict(data)
        pending_sort = getattr(panel, "sort_order", None)
        await _release_db_txn(db)
        await _probe_panel(probe)

        panel = await db.get(Panel, panel_id)
        if not panel:
            raise HTTPException(status_code=404, detail="Panel not found")
        for k, v in pending.items():
            if k == "base_url" and v:
                v = normalize_panel_base_url(str(v))
            elif k in ("username", "password", "api_token") and isinstance(v, str):
                v = v.strip() or None
            setattr(panel, k, v)
        if "category_id" in pending:
            new_cat = getattr(panel, "category_id", None)
            if new_cat != old_cat:
                panel.sort_order = (
                    pending_sort
                    if pending_sort is not None
                    else await _next_panel_sort_order(db, new_cat)
                )
        panel.last_ok_at = datetime.now(timezone.utc)

    if turning_off:
        await _prepare_panel_turn_off(db, panel.id)

    await _commit_panel_db(db, label="update_panel")
    await db.refresh(panel)
    if turning_off:
        _schedule_panel_freeze(panel.id)
    if turning_on:
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

    Off → drop from subs immediately, then disable remotes on Marzban/Sanaei/
          PasarGuard/Rebecca/Astra in the background.
    On  → background heal re-enables / provisions accounts on linked groups.
    """
    panel = await db.get(Panel, panel_id)
    if not panel:
        raise HTTPException(status_code=404, detail="Panel not found")
    was_active = bool(panel.is_active)
    panel.is_active = bool(body.is_active)
    turning_off = was_active and not bool(panel.is_active)
    turning_on = (not was_active) and bool(panel.is_active)
    if turning_off:
        await _prepare_panel_turn_off(db, panel.id)
    await db.commit()
    await db.refresh(panel)
    if turning_off:
        _schedule_panel_freeze(panel.id)
    if turning_on:
        asyncio.create_task(
            _bg_heal_panel(panel.id),
            name=f"astra-panel-heal-{panel.id}",
        )
    return _to_out(panel)


async def _prepare_panel_turn_off(db: AsyncSession, panel_id: int) -> None:
    """Local cleanup before remotes are frozen in the background."""
    from app.api.subscription import invalidate_user_feed_cache
    from app.services.orchestrator import (
        clear_panel_account_share_links,
        drop_inactive_panel_errors,
    )

    await drop_inactive_panel_errors(db, panel_id)
    uids = await clear_panel_account_share_links(db, panel_id)
    if uids:
        try:
            invalidate_user_feed_cache(*uids)
        except Exception:  # noqa: BLE001
            pass


def _schedule_panel_freeze(panel_id: int) -> None:
    asyncio.create_task(
        _bg_freeze_panel(panel_id),
        name=f"astra-panel-freeze-{panel_id}",
    )


async def _bg_freeze_panel(panel_id: int) -> None:
    from app.services.orchestrator import freeze_all_accounts_on_panel

    try:
        stats = await freeze_all_accounts_on_panel(panel_id)
        logger.info("bg freeze panel=%s stats=%s", panel_id, stats)
    except Exception:  # noqa: BLE001
        logger.exception("bg freeze panel=%s failed", panel_id)


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
    Sanaei / Rebecca / Astra node) so old and new subs lose those configs and
    remotes stay clean — even if the panel was already turned off.
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

    # Drop group claims FIRST so collect_links claim_keys exclude this panel
    # before the (possibly slow) remote deprovision finishes.
    await db.execute(sa_delete(GroupPanel).where(GroupPanel.panel_id == panel_id))
    await db.execute(sa_delete(AstraOutbound).where(AstraOutbound.panel_id == panel_id))
    await db.execute(sa_delete(AstraNodeClient).where(AstraNodeClient.panel_id == panel_id))
    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        logger.exception("delete panel=%s claim cleanup failed", panel_id)
        raise HTTPException(status_code=500, detail=f"حذف پنل ناموفق: {exc}") from exc

    # Clears links + marks deleted + commits + invalidates, then cuts remotes.
    stats = await deprovision_all_accounts_on_panel(db, panel_id)

    panel = await db.get(Panel, panel_id)
    if panel is None:
        return {
            "detail": "Panel deleted",
            "remote_deleted": stats.get("ok", 0),
            "remote_failed": stats.get("fail", 0),
            "accounts": stats.get("accounts", 0),
        }
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


def _panel_test_grade(success: bool, latency_ms: int | None) -> str:
    if not success:
        return "fail"
    if latency_ms is None:
        return "ok"
    if latency_ms <= 1000:
        return "fast"
    if latency_ms <= 3000:
        return "ok"
    return "slow"


def _panel_test_all_stats(
    results: list[PanelTestOut], *, duration_ms: int
) -> PanelTestAllStats:
    ok_n = sum(1 for r in results if r.success)
    fail_n = len(results) - ok_n
    latencies = sorted(
        int(r.latency_ms)
        for r in results
        if r.success and r.latency_ms is not None and int(r.latency_ms) >= 0
    )
    avg = int(sum(latencies) / len(latencies)) if latencies else None
    med = None
    if latencies:
        mid = len(latencies) // 2
        if len(latencies) % 2:
            med = latencies[mid]
        else:
            med = int((latencies[mid - 1] + latencies[mid]) / 2)
    return PanelTestAllStats(
        total=len(results),
        ok=ok_n,
        fail=fail_n,
        active_ok=sum(1 for r in results if r.is_active and r.success),
        active_fail=sum(1 for r in results if r.is_active and not r.success),
        inactive_ok=sum(1 for r in results if (not r.is_active) and r.success),
        inactive_fail=sum(1 for r in results if (not r.is_active) and not r.success),
        fast=sum(1 for r in results if r.grade == "fast"),
        normal=sum(1 for r in results if r.grade == "ok"),
        slow=sum(1 for r in results if r.grade == "slow"),
        avg_latency_ms=avg,
        median_latency_ms=med,
        best_latency_ms=latencies[0] if latencies else None,
        worst_latency_ms=latencies[-1] if latencies else None,
        duration_ms=int(duration_ms),
    )


@router.post("/api/admin/panels/test-all", response_model=PanelTestAllOut)
@router.post("/api/panel/test-all", response_model=PanelTestAllOut)
async def test_all_panels(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Probe every *active* panel in parallel — off panels are skipped."""
    import time as _time

    rows = await db.execute(
        select(Panel)
        .where(Panel.is_active.is_(True))
        .order_by(Panel.id.asc())
    )
    panels = list(rows.scalars().all())
    now = datetime.now(timezone.utc)
    if not panels:
        empty = PanelTestAllStats(duration_ms=0)
        return PanelTestAllOut(
            results=[], ok=0, fail=0, stats=empty, tested_at=now
        )

    # Detach probe inputs from the request AsyncSession. Concurrent workers
    # must not mutate/read the same session (that broke commit with rvf5).
    from types import SimpleNamespace

    snapshots: list[dict] = []
    for p in panels:
        probe_panel = SimpleNamespace(
            id=int(p.id),
            name=p.name,
            type=p.type,
            is_active=True,
            base_url=p.base_url,
            username=p.username,
            password=p.password,
            api_token=p.api_token,
            meta=dict(p.meta or {}),
        )
        snapshots.append(
            {
                "id": int(p.id),
                "name": p.name,
                "type": p.type,
                "is_active": True,
                "base_url": str(getattr(p, "base_url", "") or "").rstrip("/")
                or None,
                "panel": probe_panel,
            }
        )

    # Fewer concurrent probes + longer per-panel budget so slow CF/hosts
    # (e.g. real.takrofast.xyz) are not false-failed by a 7s wall clock.
    sem = asyncio.Semaphore(6)
    t0 = _time.monotonic()

    async def _one(snap: dict) -> PanelTestOut:
        async with sem:
            probe = await _live_test_panel(snap["panel"], overall_timeout=22.0)
        ok = bool(probe.get("success"))
        ms = probe.get("latency_ms")
        latency = int(ms) if isinstance(ms, (int, float)) and ms >= 0 else None
        return PanelTestOut(
            id=snap["id"],
            name=snap["name"],
            type=snap["type"],
            is_active=snap["is_active"],
            success=ok,
            error=None if ok else (probe.get("error") or "اتصال ناموفق"),
            latency_ms=latency,
            grade=_panel_test_grade(ok, latency),
            base_url=snap["base_url"],
        )

    gathered = await asyncio.gather(
        *[_one(s) for s in snapshots], return_exceptions=True
    )
    duration_ms = int((_time.monotonic() - t0) * 1000)

    results: list[PanelTestOut] = []
    ok_ids: list[int] = []
    for snap, item in zip(snapshots, gathered):
        if isinstance(item, Exception):
            logger.warning(
                "test-all panel=%s failed: %s", snap["id"], item, exc_info=False
            )
            results.append(
                PanelTestOut(
                    id=snap["id"],
                    name=snap["name"],
                    type=snap["type"],
                    is_active=snap["is_active"],
                    success=False,
                    error=(str(item).strip() or item.__class__.__name__)[:300],
                    latency_ms=None,
                    grade="fail",
                    base_url=snap["base_url"],
                )
            )
            continue
        results.append(item)
        if item.success:
            ok_ids.append(int(item.id))

    # Sequential last_ok_at update — one SQL, no concurrent session writes.
    if ok_ids:
        try:
            await db.execute(
                update(Panel)
                .where(Panel.id.in_(ok_ids))
                .values(last_ok_at=now)
            )
            await db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("test-all last_ok_at commit failed")
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass

    stats = _panel_test_all_stats(results, duration_ms=duration_ms)
    return PanelTestAllOut(
        results=results,
        ok=stats.ok,
        fail=stats.fail,
        stats=stats,
        tested_at=now,
    )


@router.post("/api/panel/{panel_id}/test")
@router.post("/api/admin/panels/{panel_id}/test")
async def test_panel(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Live test one panel — accurate, capped, token-cache cleared."""
    panel = await db.get(Panel, panel_id)
    if not panel:
        raise HTTPException(status_code=404, detail="Panel not found")
    probe = await _live_test_panel(panel, overall_timeout=22.0)
    if probe.get("success"):
        panel.last_ok_at = datetime.now(timezone.utc)
        await db.commit()
    return {
        "success": bool(probe.get("success")),
        "error": probe.get("error"),
        "latency_ms": probe.get("latency_ms"),
        "raw": probe.get("raw") or {},
    }


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
