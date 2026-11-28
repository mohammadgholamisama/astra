from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader, OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_token, verify_password
from app.models.admin import Admin
from app.models.api_key import ApiKey

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/admin/token", auto_error=False)
api_key_header = APIKeyHeader(
    name="X-Api-Key",
    auto_error=False,
    scheme_name="ApiKeyAuth",
    description=(
        "فقط خود کلید (مثل pg_key_…) — بدون کلمه Bearer. "
        "از تنظیمات → کلیدهای API کپی کنید."
    ),
)


def _normalize_api_key(raw: str | None) -> str:
    """Strip accidental Bearer/ApiKey prefixes from Swagger / copy-paste."""
    key = (raw or "").strip().strip('"').strip("'")
    if not key:
        return ""
    lower = key.lower()
    if lower.startswith("bearer "):
        key = key[7:].strip()
    elif lower.startswith("apikey "):
        key = key[7:].strip()
    return key.strip()


async def get_current_admin(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
    api_key_dep: str | None = Depends(api_key_header),
) -> Admin:
    """Auth via X-Api-Key or Bearer JWT.

    Positional callers use ``get_current_admin(request, token, db)`` — keep
    ``db`` as the 3rd arg so those sites (system/host, users) do not break.
    """
    # Manual callers pass only (request, token, db). In that case the 4th
    # default is still the Depends(...) sentinel — never call .strip() on it.
    if not isinstance(api_key_dep, str):
        api_key_dep = None
    # 1) X-Api-Key (header dependency + raw header + Authorization: ApiKey …)
    api_key = _normalize_api_key(api_key_dep) or _normalize_api_key(
        request.headers.get("X-Api-Key")
    )
    auth = request.headers.get("Authorization") or ""

    if not api_key and auth.lower().startswith("apikey "):
        api_key = _normalize_api_key(auth[7:])
    # Common mistake: Authorization: Bearer pg_key_… (treat as API key)
    if not api_key and auth.lower().startswith("bearer "):
        maybe = _normalize_api_key(auth[7:])
        if maybe.startswith("pg_key_"):
            api_key = maybe

    if api_key:
        # store hash of full key — we look up by hashing
        from hashlib import sha256

        key_hash = sha256(api_key.encode()).hexdigest()
        result = await db.execute(
            select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.is_active.is_(True))
        )
        record = result.scalar_one_or_none()
        if not record:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
        if record.expires_at and record.expires_at < datetime.now(timezone.utc):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key expired")
        admin = await db.get(Admin, record.admin_id)
        if not admin or not admin.is_active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin inactive")
        return admin

    # 2) Bearer JWT
    bearer = token
    if not bearer and auth.lower().startswith("bearer "):
        bearer = auth[7:].strip()

    if not bearer:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    payload = decode_token(bearer)
    if not payload or "sub" not in payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    result = await db.execute(select(Admin).where(Admin.username == payload["sub"]))
    admin = result.scalar_one_or_none()
    if not admin or not admin.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin not found")
    return admin


async def authenticate_admin(db: AsyncSession, username: str, password: str) -> Admin | None:
    uname = (username or "").strip()
    if not uname:
        return None
    result = await db.execute(select(Admin).where(Admin.username == uname))
    admin = result.scalar_one_or_none()
    if not admin or not admin.is_active:
        return None
    if not verify_password(password or "", admin.hashed_password):
        return None
    return admin
