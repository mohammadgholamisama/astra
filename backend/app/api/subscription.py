import base64

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.user import UserStatus
from app.services.orchestrator import (
    collect_links,
    collect_links_detailed,
    device_limit_for_user,
    sync_subscription_state,
    usage_breakdown_for_user,
)
from app.services.site_settings import (
    ensure_loaded,
    get_brand_name,
    get_link_support,
    get_public_links,
    get_sub_base_url,
)
from app.services.sub_page import render_subscription_page
from app.services.users import load_user_by_token, subscription_url

router = APIRouter(tags=["Subscription"])


def _sub_headers(user) -> dict[str, str]:
    """Client-facing usage headers — values already coefficient-weighted."""
    upload = 0
    download = int(user.used_traffic or 0)
    total = int(user.data_limit or 0)
    expire = int(user.expire or 0)
    brand = get_brand_name()
    title_name = (getattr(user, "display_name", None) or "").strip() or user.username
    support = get_link_support() or get_sub_base_url()
    return {
        "profile-title": f"{brand}-{title_name}",
        "profile-update-interval": "12",
        "subscription-userinfo": f"upload={upload}; download={download}; total={total}; expire={expire}",
        "support-url": support,
    }


def _is_browser(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    ua = (request.headers.get("user-agent") or "").lower()
    if "v2ray" in ua or "clash" in ua or "sing-box" in ua or "hiddify" in ua or "streisand" in ua:
        return False
    return "text/html" in accept


@router.get("/sub/{token}")
async def get_subscription(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await load_user_by_token(db, token)
    if not user:
        return PlainTextResponse("Not found", status_code=404)

    # Browser → pretty HTML page (unless ?raw=1 for client import)
    raw = (request.query_params.get("raw") or "").lower() in ("1", "true", "yes")
    if _is_browser(request) and not raw:
        return await subscription_page(token, db)

    # Mutates user in-place; expire_on_commit=False → no second DB round-trip
    await sync_subscription_state(db, user)
    await db.commit()

    headers = _sub_headers(user)

    # Exhausted / expired / disabled → empty feed so clients drop configs
    if user.status not in (UserStatus.active, UserStatus.on_hold):
        return Response(content="", media_type="text/plain", headers=headers)

    links = await collect_links(user)
    content = base64.b64encode("\n".join(links).encode()).decode()
    return Response(content=content, media_type="text/plain", headers=headers)


@router.get("/sub/{token}/info", response_class=HTMLResponse)
async def subscription_page(token: str, db: AsyncSession = Depends(get_db)):
    user = await load_user_by_token(db, token)
    if not user:
        return HTMLResponse("<h1>Not found</h1>", status_code=404)

    await ensure_loaded(db)
    await sync_subscription_state(db, user)
    await db.commit()

    configs = collect_links_detailed(user)
    panels_usage = await usage_breakdown_for_user(db, user)
    device_limit = await device_limit_for_user(db, user)
    status = user.status.value if hasattr(user.status, "value") else str(user.status)
    show_name = (getattr(user, "display_name", None) or "").strip() or user.username
    html = render_subscription_page(
        panel_name=get_brand_name(),
        username=show_name,
        status=status,
        used=int(user.used_traffic or 0),
        limit=int(user.data_limit or 0),
        expire=user.expire,
        sub_url=subscription_url(user),
        configs=configs,
        panels_usage=panels_usage,
        device_limit=device_limit,
        public_links=get_public_links(),
    )
    return HTMLResponse(html)
