import asyncio
import base64
import logging
import re

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
logger = logging.getLogger("astra.sub")

# Known subscription / proxy client User-Agents (never serve HTML to these)
_CLIENT_UA = re.compile(
    r"v2ray|clash|sing-?box|hiddify|streisand|nekobox|nekoray|shadowrocket|"
    r"quantumult|surge|loon|stash|okhttp|go-http-client|dart[:/]|cfnetwork|"
    r"v2box|foxray|napsternet|oneclick|sfa|sfi|sfm|surface|karing|flclash|"
    r"happ|v2raytun|streisand|pharos|proxy|vpn",
    re.I,
)
_BROWSER_UA = re.compile(
    r"mozilla/|chrome/|safari/|firefox/|edg/|opr/|crios/|fxios/",
    re.I,
)


def _header_base64(text: str) -> str:
    """Encode non-ASCII profile titles the way v2rayNG / Clash expect."""
    raw = (text or "").strip() or "Astra"
    return "base64:" + base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _sub_headers(user) -> dict[str, str]:
    """Client-facing usage headers — values already coefficient-weighted."""
    upload = 0
    download = int(user.used_traffic or 0)
    total = int(user.data_limit or 0)
    expire = int(user.expire or 0)
    brand = get_brand_name()
    title_name = (getattr(user, "display_name", None) or "").strip() or user.username
    support = (get_link_support() or get_sub_base_url() or "").strip()
    headers = {
        "profile-title": _header_base64(f"{brand}-{title_name}"),
        "profile-update-interval": "12",
        "subscription-userinfo": f"upload={upload}; download={download}; total={total}; expire={expire}",
        # Help some clients / CDNs
        "content-disposition": 'attachment; filename="Astra"',
    }
    if support.startswith("http://") or support.startswith("https://"):
        headers["support-url"] = support
    return headers


def _wants_html_page(request: Request) -> bool:
    """
    Only real browsers get the pretty HTML page.
    Default = subscription feed (base64). False positives break client import.
    """
    raw = (request.query_params.get("raw") or "").lower()
    if raw in ("1", "true", "yes"):
        return False
    html_q = (request.query_params.get("html") or "").lower()
    if html_q in ("1", "true", "yes"):
        return True

    ua = request.headers.get("user-agent") or ""
    if not ua.strip():
        return False
    if _CLIENT_UA.search(ua):
        return False
    if not _BROWSER_UA.search(ua):
        return False

    accept = (request.headers.get("accept") or "").lower()
    # Prefer HTML only when Accept explicitly asks for it
    if "text/html" not in accept:
        return False
    return True


def _encode_feed(links: list[str]) -> str:
    """Standard base64 subscription body (one share-link per line)."""
    body = "\n".join(x.strip() for x in links if (x or "").strip())
    if not body:
        return ""
    return base64.b64encode(body.encode("utf-8")).decode("ascii")


@router.get("/sub/{token}")
async def get_subscription(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await load_user_by_token(db, token)
    if not user:
        return PlainTextResponse("Not found", status_code=404)

    if _wants_html_page(request):
        return await subscription_page(token, db)

    # Fast path for V2Ray clients: never block long on remote usage sync.
    # Cached links + headers first; usage refresh is best-effort ≤2s.
    try:
        await asyncio.wait_for(sync_subscription_state(db, user, force=False), timeout=2.0)
        await db.commit()
    except asyncio.TimeoutError:
        logger.warning("sub sync timeout user=%s — serving cached links", user.username)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("sub sync error user=%s: %s — serving cached links", user.username, exc)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass

    headers = _sub_headers(user)

    if user.status not in (UserStatus.active, UserStatus.on_hold):
        return Response(
            content="",
            media_type="text/plain; charset=utf-8",
            headers=headers,
        )

    # Client feed: real share links only (no fake notice configs — they break imports)
    links = await collect_links(user, include_notices=False)
    content = _encode_feed(links)
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers=headers,
    )


@router.get("/sub/{token}/info", response_class=HTMLResponse)
async def subscription_page(token: str, db: AsyncSession = Depends(get_db)):
    user = await load_user_by_token(db, token)
    if not user:
        return HTMLResponse("<h1>Not found</h1>", status_code=404)

    await ensure_loaded(db)
    try:
        await asyncio.wait_for(sync_subscription_state(db, user, force=True), timeout=8.0)
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("sub page sync: %s", exc)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass

    configs = collect_links_detailed(user, include_notices=True)
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
