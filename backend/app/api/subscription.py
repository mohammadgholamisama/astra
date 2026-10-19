import asyncio
import base64
import logging
import re
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal, get_db
from app.models.user import PanelAccount, User, UserStatus
from app.services.orchestrator import (
    collect_links,
    collect_links_detailed,
    device_limit_for_user,
    enforce_user_quota,
    hydrate_share_links,
    refresh_usage,
    sync_modify_user,
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

# Debounce background maintain so many clients don't stampede one user
_bg_last: dict[int, float] = {}
_BG_MIN_INTERVAL = 25.0


def _account_has_ready_shares(account) -> bool:
    """True when account can contribute stable share links to the feed."""
    from app.adapters.http_utils import is_share_link, is_subscription_ref
    from app.models.user import PanelAccountStatus
    from app.services.orchestrator import _panel_is_active

    if account.status not in (
        PanelAccountStatus.active,
        PanelAccountStatus.error,
        PanelAccountStatus.pending,
    ):
        return True  # not expected in feed — ignore
    panel = account.__dict__.get("panel") or getattr(account, "panel", None)
    if panel is not None and not _panel_is_active(panel):
        return True  # filtered out of feed
    links = [str(x).strip() for x in (account.links or []) if x and str(x).strip()]
    if any(is_subscription_ref(x) for x in links):
        return False  # still need expand
    return any(is_share_link(x) for x in links)


def _user_has_share_links(user) -> bool:
    """Legacy: any account has at least one share link."""
    from app.adapters.http_utils import is_share_link

    for account in user.panel_accounts or []:
        for link in account.links or []:
            if is_share_link(str(link or "")):
                return True
    return False


async def _user_sub_links_complete(db: AsyncSession, user) -> bool:
    """
    Every panel×group the user should see already has expanded share links.

    Unlike ``_user_has_share_links`` (OR across accounts), this is AND across
    membership — prevents serving half a multi-panel sub.
    """
    from app.services.orchestrator import resolve_group_panel_claims

    claims = await resolve_group_panel_claims(db, user)
    if not claims:
        return True

    expected: set[tuple[int, int | None]] = set()
    for c in claims:
        expected.add((int(c["panel_id"]), int(c["group_id"])))

    by_key: dict[tuple[int, int], object] = {}
    legacy_by_panel: dict[int, object] = {}
    for account in user.panel_accounts or []:
        gid = getattr(account, "group_id", None)
        if gid is not None:
            by_key[(int(account.panel_id), int(gid))] = account
        else:
            legacy_by_panel[int(account.panel_id)] = account

    for pid, gid in expected:
        account = by_key.get((pid, int(gid))) or legacy_by_panel.get(pid)
        if account is None:
            # Missing provision — don't block forever; bg maintain will create.
            continue
        if not _account_has_ready_shares(account):
            return False
    return True


async def _bg_sub_maintain(user_id: int) -> None:
    """Fill missing share links + soft usage refresh — never on the HTTP path."""
    now = time.monotonic()
    last = _bg_last.get(user_id, 0.0)
    if now - last < _BG_MIN_INTERVAL:
        return
    _bg_last[user_id] = now
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User)
                .where(User.id == user_id)
                .options(
                    selectinload(User.panel_accounts).selectinload(PanelAccount.panel),
                    selectinload(User.groups),
                )
            )
            user = result.scalar_one_or_none()
            if not user:
                return
            # If some panels failed at create time, retry them when sub is opened
            # so configs appear automatically once the panel is reachable again.
            try:
                from app.models.user import PanelAccountStatus
                from app.services.orchestrator import retry_failed_accounts

                has_fail = any(
                    a.status
                    in (PanelAccountStatus.error, PanelAccountStatus.pending)
                    for a in (user.panel_accounts or [])
                )
                if has_fail:
                    await asyncio.wait_for(
                        retry_failed_accounts(db, user), timeout=20.0
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("bg heal user_id=%s: %s", user_id, exc)
            try:
                await asyncio.wait_for(hydrate_share_links(db, user), timeout=6.0)
            except Exception as exc:  # noqa: BLE001
                logger.warning("bg hydrate user_id=%s: %s", user_id, exc)
            try:
                # quick=True → no provision, hard overall budget on panel pulls
                await asyncio.wait_for(
                    refresh_usage(db, user, force=False, quick=True),
                    timeout=5.5,
                )
                await enforce_user_quota(db, user)
            except Exception as exc:  # noqa: BLE001
                logger.warning("bg usage user_id=%s: %s", user_id, exc)
            try:
                await db.commit()
            except Exception:  # noqa: BLE001
                await db.rollback()
    except Exception:  # noqa: BLE001
        logger.exception("bg sub maintain user_id=%s failed", user_id)


def _schedule_bg_maintain(user_id: int) -> None:
    try:
        asyncio.create_task(
            _bg_sub_maintain(user_id),
            name=f"astra-sub-bg-{user_id}",
        )
    except RuntimeError:
        pass


def _header_base64(text: str) -> str:
    raw = (text or "").strip() or "Astra"
    return "base64:" + base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _safe_filename(name: str) -> str:
    """ASCII-only — HTTP headers are latin-1 (Persian filename caused 500)."""
    raw = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "Astra").strip())
    return (raw[:64].strip("._") or "Astra")


def _sub_headers(user) -> dict[str, str]:
    """Feed headers (PasarGuard-compatible)."""
    upload = 0
    download = int(user.used_traffic or 0)
    total = int(user.data_limit or 0)
    expire = int(user.expire or 0)
    brand = get_brand_name()
    title_name = (getattr(user, "display_name", None) or "").strip() or user.username
    file_base = _safe_filename(user.username or "Astra")
    support = (get_link_support() or get_sub_base_url() or "").strip()
    sub = subscription_url(user)
    headers = {
        "profile-title": _header_base64(f"{brand}-{title_name}"),
        # 1h so remark/tag changes (group config_tag) reach clients quickly
        "profile-update-interval": "1",
        "subscription-userinfo": f"upload={upload}; download={download}; total={total}; expire={expire}",
        # filename ASCII only; some clients mishandle attachment — keep for PG parity
        "content-disposition": f'attachment; filename="{file_base}"',
        "cache-control": "no-store",
        "profile-web-page-url": sub,
    }

    if support.startswith("http://") or support.startswith("https://"):
        headers["support-url"] = support
    return headers


def _wants_html_page(request: Request) -> bool:
    """
    HTML only for real browser *navigation* (Sec-Fetch). Default = Base64 feed.

    V2Ray apps often use WebView/OkHttp with Mozilla UA + Accept:text/html and
    NO Sec-Fetch — those must get the feed, not the info page.
    Desktop/mobile browsers always send Sec-Fetch-Mode: navigate on URL open.
    Force HTML with ?html=1; force feed with ?raw=1.
    """
    raw = (request.query_params.get("raw") or "").lower()
    if raw in ("1", "true", "yes"):
        return False
    html_q = (request.query_params.get("html") or "").lower()
    if html_q in ("1", "true", "yes"):
        return True

    sec_dest = (request.headers.get("sec-fetch-dest") or "").lower()
    sec_mode = (request.headers.get("sec-fetch-mode") or "").lower()
    # Only address-bar / link navigation → HTML. Everything else → feed.
    if sec_dest in ("document", "iframe") or sec_mode == "navigate":
        return True
    return False


def _encode_feed(links: list[str]) -> bytes:
    """Base64 subscription body as bytes (enables explicit Content-Length)."""
    body = "\n".join(x.strip() for x in links if (x or "").strip())
    if not body:
        return b""
    return base64.b64encode(body.encode("utf-8"))


def _feed_response(body: bytes, headers: dict[str, str]) -> Response:
    """Plain feed with explicit Content-Length (avoid Transfer-Encoding: chunked)."""
    hdrs = {**headers, "content-length": str(len(body))}
    # Ensure we don't advertise chunked
    hdrs.pop("transfer-encoding", None)
    hdrs.pop("Transfer-Encoding", None)
    return Response(
        content=body,
        status_code=200,
        media_type="text/plain; charset=utf-8",
        headers=hdrs,
    )


async def _ensure_share_links(
    db: AsyncSession,
    token: str,
    user,
    *,
    timeout: float = 4.0,
    force: bool = False,
):
    """Hydrate missing share links with a hard budget (never block multi-panel users)."""
    if not force and await _user_sub_links_complete(db, user):
        # Every membership account already has expanded shares — serve immediately
        links = await collect_links(user, include_notices=False, db=db)
        return user, links
    try:
        n = await asyncio.wait_for(hydrate_share_links(db, user), timeout=timeout)
        if n:
            await db.commit()
            user = await load_user_by_token(db, token) or user
            logger.info("sub hydrated user=%s accounts=%s", user.username, n)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sub hydrate user=%s: %s", getattr(user, "username", "?"), exc)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        user = await load_user_by_token(db, token) or user
    links = await collect_links(user, include_notices=False, db=db)
    if not links:
        logger.warning(
            "sub feed empty user=%s status=%s accounts=%s",
            getattr(user, "username", "?"),
            getattr(user, "status", None),
            len(user.panel_accounts or []),
        )
    return user, links


async def _pull_live_usage(db: AsyncSession, user, *, push: bool = False) -> None:
    """
    Pull used_traffic from remotes, apply Σ(raw×coeff), enforce Astra quota, commit.

    Always pushes remotes when Astra is limited/expired (even if push=False) so
    upstream configs turn off when the sale finishes earlier than the main panel.
    Multi-panel / coefficient users also get remaining-cap pushes when push=True
    or when status just flipped.
    """
    from app.models.user import UserStatus
    from app.services.orchestrator import should_push_remotes

    status_changed = False
    try:
        # Parallel panel pulls need headroom (Marzban Cloud / multi-panel groups)
        await asyncio.wait_for(refresh_usage(db, user, force=True), timeout=28.0)
        status_changed = await enforce_user_quota(db, user)
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("sub usage refresh: %s", exc)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return

    blocked = user.status in (
        UserStatus.limited,
        UserStatus.expired,
        UserStatus.disabled,
    )
    try:
        need = push or status_changed or blocked
        if not need:
            # Multi-panel: shrink sibling caps as usage accrues
            need = await should_push_remotes(
                db,
                user,
                status_changed=False,
                force=False,
                shrink_shared_caps=True,
            )
        if not need:
            return
        await asyncio.wait_for(sync_modify_user(db, user), timeout=15.0)
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("sub usage push: %s", exc)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass


async def _render_info_page(db: AsyncSession, token: str, user) -> HTMLResponse:
    await ensure_loaded(db)
    # Same policy as the feed: complete cached links, hydrate gaps, refresh in BG.
    # Do NOT run request-time refresh_usage here — partial panel responses used to
    # shrink stored links and make HTML/feed counts diverge.
    if await _user_sub_links_complete(db, user):
        user = await load_user_by_token(db, token) or user
    else:
        user, _links = await _ensure_share_links(
            db, token, user, timeout=3.5, force=True
        )
    _schedule_bg_maintain(int(user.id))

    configs = await collect_links_detailed(user, include_notices=True, db=db)
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
    return HTMLResponse(
        html,
        headers={
            "cache-control": "no-store",
            "content-disposition": "inline",
        },
    )


@router.get("/sub/{token}")
async def get_subscription(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    One URL, two responses (like PasarGuard):
    - Browser (Mozilla + Accept:text/html) → HTML page
    - Everything else → Base64 feed with Content-Length

    Feed path is cache-first when EVERY membership account has ready share links.
    Incomplete accounts get a short hydrate, then we serve; BG maintain fills gaps.
    """
    user = await load_user_by_token(db, token)
    if not user:
        return PlainTextResponse("Not found", status_code=404)

    if _wants_html_page(request):
        return await _render_info_page(db, token, user)

    from app.services.site_settings import ensure_loaded

    await ensure_loaded(db)

    # Complete membership → serve immediately (stable count).
    # Incomplete → short hydrate so multi-panel users don't get half a feed.
    if await _user_sub_links_complete(db, user):
        links = await collect_links(user, include_notices=False, db=db)
        _schedule_bg_maintain(int(user.id))
    else:
        user, links = await _ensure_share_links(
            db, token, user, timeout=4.0, force=True
        )
        _schedule_bg_maintain(int(user.id))

    headers = _sub_headers(user)

    if user.status not in (UserStatus.active, UserStatus.on_hold):
        return _feed_response(b"", headers)

    body = _encode_feed(links)
    if not body:
        logger.warning("sub returning empty body user=%s token=%s…", user.username, token[:8])

    return _feed_response(body, headers)


@router.get("/sub/{token}/info", response_class=HTMLResponse)
async def subscription_page(token: str, db: AsyncSession = Depends(get_db)):
    user = await load_user_by_token(db, token)
    if not user:
        return HTMLResponse("<h1>Not found</h1>", status_code=404)
    return await _render_info_page(db, token, user)
