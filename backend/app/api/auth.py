from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import authenticate_admin, get_current_admin, oauth2_scheme
from app.core.database import get_db
from app.core.security import create_access_token, hash_password, verify_password
from app.models.admin import Admin
from app.schemas.auth import AdminOut, Token

router = APIRouter(tags=["Admin"])


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class ChangePasswordOut(BaseModel):
    ok: bool = True
    message: str = "رمز عبور تغییر کرد"


async def _session_admin(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> Admin:
    """Password change: JWT panel session only (block API keys)."""
    if request.headers.get("X-Api-Key"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "SESSION_REQUIRED", "message": "فقط با ورود به پنل"},
        )
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("apikey "):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "SESSION_REQUIRED", "message": "فقط با ورود به پنل"},
        )
    bearer = token
    if not bearer and auth.lower().startswith("bearer "):
        bearer = auth[7:].strip()
    if not bearer:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    from app.core.security import decode_token

    payload = decode_token(bearer)
    if not payload or "sub" not in payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    result = await db.execute(select(Admin).where(Admin.username == payload["sub"]))
    admin = result.scalar_one_or_none()
    if not admin or not admin.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin not found")
    return admin


@router.post("/api/admin/token", response_model=Token)
async def login_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    admin = await authenticate_admin(db, form_data.username, form_data.password)
    if not admin:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )
    token = create_access_token(admin.username)
    return Token(access_token=token)


@router.get("/api/admin", response_model=AdminOut)
async def get_admin(admin=Depends(get_current_admin)):
    return AdminOut(username=admin.username, is_sudo=admin.is_sudo)


@router.post("/api/admin/change-password", response_model=ChangePasswordOut)
async def change_password(
    body: ChangePasswordIn,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(_session_admin),
):
    if not verify_password(body.current_password, admin.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_PASSWORD", "message": "رمز فعلی اشتباه است"},
        )
    new_pw = (body.new_password or "").strip()
    if len(new_pw) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "WEAK_PASSWORD", "message": "رمز جدید حداقل ۸ کاراکتر باشد"},
        )
    if new_pw == body.current_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "SAME_PASSWORD", "message": "رمز جدید باید با رمز فعلی فرق داشته باشد"},
        )
    admin.hashed_password = hash_password(new_pw)
    await db.commit()
    return ChangePasswordOut(message="رمز عبور با موفقیت تغییر کرد")
