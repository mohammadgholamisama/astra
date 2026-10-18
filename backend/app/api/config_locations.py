from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin
from app.core.database import get_db
from app.models.config_location import ConfigLocation
from app.schemas.config_location import (
    ConfigLocationCreate,
    ConfigLocationEndpointSet,
    ConfigLocationOut,
    ConfigLocationUpdate,
)
from app.services.config_locations import (
    flag_from_country_code,
    normalize_country_code,
    normalize_host,
    normalize_label,
    normalize_match_host,
    refresh_cache,
    to_out,
    upsert_endpoint_location,
)

router = APIRouter(tags=["Config Locations"])


def _as_out(row: ConfigLocation) -> ConfigLocationOut:
    return ConfigLocationOut(**to_out(row))


@router.get("/api/admin/config-locations", response_model=list[ConfigLocationOut])
async def list_locations(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    result = await db.execute(
        select(ConfigLocation).order_by(
            ConfigLocation.host.asc(),
            ConfigLocation.port.asc(),
            ConfigLocation.match_host.asc(),
        )
    )
    return [_as_out(r) for r in result.scalars().all()]


@router.post("/api/admin/config-locations", response_model=ConfigLocationOut)
async def create_location(
    body: ConfigLocationCreate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    try:
        host = normalize_host(body.host)
        code = normalize_country_code(body.country_code)
        flag_from_country_code(code)  # validate
        match_host = normalize_match_host(body.match_host)
        label = normalize_label(body.label)
        port = int(body.port)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    row = ConfigLocation(
        host=host,
        port=port,
        match_host=match_host,
        country_code=code,
        label=label,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="برای این دامنه، پورت و هاست هدر قبلاً لوکیشن ثبت شده",
        ) from exc
    await db.refresh(row)
    await refresh_cache(db)
    return _as_out(row)


@router.put("/api/admin/config-locations/endpoint")
async def set_endpoint_location(
    body: ConfigLocationEndpointSet,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Set or clear flag for exact host:port[+match_host].

    Must be registered BEFORE ``/{location_id}`` so ``endpoint`` is not parsed as an int.
    """
    try:
        out = await upsert_endpoint_location(
            db,
            host=body.host,
            port=int(body.port),
            match_host=body.match_host or "",
            country_code=body.country_code,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="برای این دامنه، پورت و هاست هدر قبلاً لوکیشن ثبت شده",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(
            status_code=500,
            detail=f"خطا در تنظیم پرچم: {exc}",
        ) from exc
    # Serialize datetimes so JSON never fails on plain dict responses
    if out and isinstance(out, dict):
        for key in ("created_at", "updated_at"):
            val = out.get(key)
            if hasattr(val, "isoformat"):
                out[key] = val.isoformat()
    return {"ok": True, "location": out}


@router.put("/api/admin/config-locations/{location_id}", response_model=ConfigLocationOut)
async def update_location(
    location_id: int,
    body: ConfigLocationUpdate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    row = await db.get(ConfigLocation, location_id)
    if not row:
        raise HTTPException(status_code=404, detail="لوکیشن پیدا نشد")
    data = body.model_dump(exclude_unset=True)
    try:
        if "host" in data and data["host"] is not None:
            row.host = normalize_host(data["host"])
        if "port" in data and data["port"] is not None:
            row.port = int(data["port"])
        if "match_host" in data:
            row.match_host = normalize_match_host(data["match_host"])
        if "country_code" in data and data["country_code"] is not None:
            code = normalize_country_code(data["country_code"])
            flag_from_country_code(code)
            row.country_code = code
        if "label" in data:
            row.label = normalize_label(data["label"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="برای این دامنه، پورت و هاست هدر قبلاً لوکیشن ثبت شده",
        ) from exc
    await db.refresh(row)
    await refresh_cache(db)
    return _as_out(row)


@router.delete("/api/admin/config-locations/{location_id}")
async def delete_location(
    location_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    row = await db.get(ConfigLocation, location_id)
    if not row:
        raise HTTPException(status_code=404, detail="لوکیشن پیدا نشد")
    await db.delete(row)
    await db.commit()
    await refresh_cache(db)
    return {"detail": "deleted"}
