"""Admin site settings — JWT session only (not API keys)."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import oauth2_scheme
from app.core.database import get_db
from app.core.security import decode_token
from app.models.admin import Admin
from app.schemas.settings import (
    SiteSettingsOut,
    SiteSettingsUpdate,
    SslIssueRequest,
    SslIssueResponse,
)
from app.services import site_settings as site_svc
from app.services import ssl_ops

router = APIRouter(tags=["Settings"])


async def get_session_admin_only(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> Admin:
    """Settings are sensitive — block API keys, require panel JWT."""
    if request.headers.get("X-Api-Key"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "SESSION_REQUIRED",
                "message": "تغییر تنظیمات فقط با ورود به پنل مجاز است",
            },
        )
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("apikey "):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "SESSION_REQUIRED",
                "message": "تغییر تنظیمات فقط با ورود به پنل مجاز است",
            },
        )

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


@router.get("/api/admin/settings", response_model=SiteSettingsOut)
async def get_settings_api(
    db: AsyncSession = Depends(get_db),
    _admin: Admin = Depends(get_session_admin_only),
):
    data = await site_svc.read_settings(db)
    return SiteSettingsOut(**data)


@router.put("/api/admin/settings", response_model=SiteSettingsOut)
async def put_settings_api(
    body: SiteSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    _admin: Admin = Depends(get_session_admin_only),
):
    try:
        data = await site_svc.update_settings(
            db,
            sub_base_url=body.sub_base_url,
            panel_base_url=body.panel_base_url,
            brand_name=body.brand_name,
            link_channel=body.link_channel,
            link_support=body.link_support,
            link_app=body.link_app,
            config_brand_tag=body.config_brand_tag,
            promo_config_enabled=body.promo_config_enabled,
            promo_config_text=body.promo_config_text,
            promo_config_text_2=body.promo_config_text_2,
            status_line_template=body.status_line_template,
            status_panel_label=body.status_panel_label,
            reset_sub_base_url=body.reset_sub_base_url,
            reset_panel_base_url=body.reset_panel_base_url,
            reset_brand_name=body.reset_brand_name,
            clear_link_channel=body.clear_link_channel,
            clear_link_support=body.clear_link_support,
            clear_link_app=body.clear_link_app,
            clear_config_brand_tag=body.clear_config_brand_tag,
            clear_promo_config=body.clear_promo_config,
            reset_status_line=body.reset_status_line,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_SETTINGS", "message": str(exc)},
        ) from exc
    return SiteSettingsOut(**data)


@router.post("/api/admin/settings/ssl", response_model=SslIssueResponse)
async def issue_ssl_api(
    body: SslIssueRequest,
    db: AsyncSession = Depends(get_db),
    _admin: Admin = Depends(get_session_admin_only),
):
    try:
        result = await ssl_ops.issue_ssl(db, target=body.target, domain=body.domain)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "SSL_INVALID", "message": str(exc)},
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "SSL_FAILED", "message": str(exc)},
        ) from exc
    return SslIssueResponse(**result)
