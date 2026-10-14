import secrets
from hashlib import sha256

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin
from app.core.database import get_db
from app.models.admin import Admin
from app.models.api_key import ApiKey
from app.schemas.api_key import ApiKeyCreate, ApiKeyCreated, ApiKeyOut

router = APIRouter(tags=["API Keys"])


@router.post("/api/admin/api-keys", response_model=ApiKeyCreated)
async def create_api_key(
    body: ApiKeyCreate,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    raw = f"pg_key_{secrets.token_urlsafe(32)}"
    record = ApiKey(
        name=body.name,
        note=body.note,
        expires_at=body.expires_at,
        key_hash=sha256(raw.encode()).hexdigest(),
        key_prefix=raw[:12],
        admin_id=admin.id,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return ApiKeyCreated(
        id=record.id,
        name=record.name,
        key_prefix=record.key_prefix,
        note=record.note,
        is_active=record.is_active,
        expires_at=record.expires_at,
        created_at=record.created_at,
        key=raw,
    )


@router.get("/api/admin/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    result = await db.execute(select(ApiKey).where(ApiKey.admin_id == admin.id).order_by(ApiKey.id.desc()))
    return [ApiKeyOut.model_validate(k) for k in result.scalars().all()]


@router.delete("/api/admin/api-keys/{key_id}")
async def delete_api_key(
    key_id: int,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    result = await db.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.admin_id == admin.id))
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="API key not found")
    await db.delete(record)
    await db.commit()
    return {"detail": "API key deleted"}
