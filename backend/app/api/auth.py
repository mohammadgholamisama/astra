from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import authenticate_admin, get_current_admin
from app.core.database import get_db
from app.core.security import create_access_token
from app.schemas.auth import AdminOut, Token

router = APIRouter(tags=["Admin"])


@router.post("/api/admin/token", response_model=Token)
async def login_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    admin = await authenticate_admin(db, form_data.username, form_data.password)
    if not admin:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect username or password")
    token = create_access_token(admin.username)
    return Token(access_token=token)


@router.get("/api/admin", response_model=AdminOut)
async def get_admin(admin=Depends(get_current_admin)):
    return AdminOut(username=admin.username, is_sudo=admin.is_sudo)
