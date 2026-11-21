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
    activate_on_hold_if_used,
    collect_links,
    collect_links_detailed,
    device_limit_for_user,
    enforce_user_quota,
    hydrate_share_links,
    refresh_usage,
    sync_modify_user,
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


# #region agent log
def _dbg(hypothesis_id: str, location: str, message: str, data: dict | None = None) -> None:
    """Debug-mode NDJSON for session 941288 (usage miss + sub 502)."""
    import json as _json

    payload = {
        "sessionId": "941288",
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data or {},
        "timestamp": int(time.time() * 1000),
    }
    try:
        with open(
            "/opt/astra/data/debug-941288.ndjson", "a", encoding="utf-8"
        ) as f:
            f.write(_json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
    try:
        logger.info("DBG941288 %s %s %s", hypothesis_id, message, data or {})
    except Exception:
        pass


# #endregion


async def _safe_rollback(db: AsyncSession | None) -> None:
    if db is None:
        return
    try:
        await db.rollback()
    except Exception:  # noqa: BLE001
        pass


def _is_cancel(exc: BaseException) -> bool:
    return isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit))

# Debounce background maintain so many clients don't stampede one user
_bg_last: dict[int, float] = {}
_BG_MIN_INTERVAL = 12.0
# Parallel heal/hydrate — keep low so QueuePool keeps headroom for /sub.
# (Was 6; each task used to hold a DB checkout across remote HTTP.)
_bg_heavy = asyncio.Semaphore(2)

# Live usage pull on sub refresh — must stay accurate (quota / money).
_bg_usage_last: dict[int, float] = {}
_BG_USAGE_INTERVAL = 8.0
# Shares orchestrator._USAGE_PULL_GATE; keep at 1 under pool pressure.
_bg_usage_sem = asyncio.Semaphore(1)

# Tiny in-process feed cache — VPN clients often refresh in bursts.
# Incomplete coverage also caches (shorter TTL) so Iran-edge clients don't
# wait on a full rebuild every tap of «Update» while bg heal catches up.
# Cache key: (user_id, include_hidden, op_bucket) — op_bucket separates ISP orders
_feed_cache: dict[tuple[int, bool, str], tuple[float, bytes, bool]] = {}
_FEED_CACHE_TTL = 12.0
_FEED_CACHE_TTL_PARTIAL = 6.0
_FEED_CACHE_MAX = 800


def _op_bucket(rank_operator: str | None) -> str:
    from app.services.config_orders import to_ping_operator

    return to_ping_operator(rank_operator) or "global"


def _feed_cache_get(
    user_id: int, include_hidden: bool, rank_operator: str | None = None
) -> bytes | None:
    key = (int(user_id), bool(include_hidden), _op_bucket(rank_operator))
    hit = _feed_cache.get(key)
    if not hit:
        return None
    ts, body, complete = hit
    ttl = _FEED_CACHE_TTL if complete else _FEED_CACHE_TTL_PARTIAL
    if time.monotonic() - ts > ttl:
        _feed_cache.pop(key, None)
        return None
    return body


def _feed_cache_put(
    user_id: int,
    include_hidden: bool,
    body: bytes,
    *,
    complete: bool = True,
    rank_operator: str | None = None,
) -> None:
    if len(_feed_cache) >= _FEED_CACHE_MAX:
        # Drop oldest ~10%
        for k, _ in sorted(_feed_cache.items(), key=lambda kv: kv[1][0])[
            : max(1, _FEED_CACHE_MAX // 10)
        ]:
            _feed_cache.pop(k, None)
    _feed_cache[(int(user_id), bool(include_hidden), _op_bucket(rank_operator))] = (
        time.monotonic(),
        body,
        bool(complete),
    )


def invalidate_user_feed_cache(*user_ids: int) -> None:
    """Drop cached sub bodies after membership / remote changes."""
    if not user_ids:
        return
    want = {int(u) for u in user_ids if u is not None}
    for key in list(_feed_cache.keys()):
        if key[0] in want:
            _feed_cache.pop(key, None)


def clear_feed_cache() -> None:
    """Drop all feed bodies (e.g. after ISP order maps refresh)."""
    _feed_cache.clear()


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
            return False  # missing provision — bg maintain must create
        if not _account_has_ready_shares(account):
            return False
    return True


async def _bg_sub_maintain(user_id: int, *, force: bool = False) -> None:
    """Fill missing remotes/share links + soft usage refresh — never on the HTTP path.

    Uses pool-safe heal/hydrate so DB connections are not held during remote I/O
    (that was filling QueuePool → «Temporarily unavailable»).
    """
    now = time.monotonic()
    last = _bg_last.get(user_id, 0.0)
    if not force and now - last < _BG_MIN_INTERVAL:
        return
    _bg_last[user_id] = now
    try:
        async with _bg_heavy:
            from app.models.user import UserStatus
            from app.services.orchestrator import (
                heal_user_coverage_pool_safe,
                hydrate_share_links_pool_safe,
            )

            complete = True
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(User)
                    .where(User.id == user_id)
                    .options(
                        selectinload(User.panel_accounts).selectinload(
                            PanelAccount.panel
                        ),
                        selectinload(User.groups),
                    )
                )
                user = result.scalar_one_or_none()
                if not user:
                    return
                complete = await _user_sub_links_complete(db, user)
                await db.commit()
            # Session closed before remote heal/hydrate.

            if not complete:
                try:
                    await asyncio.wait_for(
                        heal_user_coverage_pool_safe(
                            int(user_id), wait=14.0, include_empty_links=True
                        ),
                        timeout=25.0,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("bg heal user_id=%s: %s", user_id, exc)
                try:
                    await asyncio.wait_for(
                        hydrate_share_links_pool_safe(int(user_id)), timeout=14.0
                    )
                    invalidate_user_feed_cache(int(user_id))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("bg hydrate user_id=%s: %s", user_id, exc)

            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(User)
                    .where(User.id == user_id)
                    .options(
                        selectinload(User.panel_accounts).selectinload(
                            PanelAccount.panel
                        ),
                        selectinload(User.groups),
                    )
                )
                user = result.scalar_one_or_none()
                if not user:
                    return
                try:
                    # Coverage maintain only — live usage is handled by
                    # ``_bg_usage_pull`` so a slow get_usage cannot block heal/hydrate.
                    activated = await activate_on_hold_if_used(db, user)
                    changed = bool(activated) or await enforce_user_quota(db, user)
                    blocked = user.status in (
                        UserStatus.limited,
                        UserStatus.expired,
                        UserStatus.disabled,
                    )
                    if changed or blocked:
                        await asyncio.wait_for(
                            sync_modify_user(
                                db,
                                user,
                                skip_nginx_sync=True,
                                freeze_fast=True,
                            ),
                            timeout=12.0,
                        )
                        invalidate_user_feed_cache(int(user_id))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("bg quota user_id=%s: %s", user_id, exc)
                try:
                    await db.commit()
                except Exception:  # noqa: BLE001
                    await db.rollback()
    except Exception:  # noqa: BLE001
        logger.exception("bg sub maintain user_id=%s failed", user_id)


async def _bg_usage_pull(user_id: int) -> None:
    """Pull live remote usage into PanelAccount + User.used_traffic (weighted).

    Runs after /sub returns so the tunnel stays fast, but every active client
    refresh still advances quota / analytics accurately. When Astra quota is
    exhausted, freezes ALL remotes in the same task.

    Uses ``refresh_usage_pool_safe`` so the DB connection is not held during
    remote HTTP (that was starving QueuePool → «Temporarily unavailable»).
    """
    from app.services.orchestrator import (
        _USAGE_PULL_GATE,
        mark_live_usage_priority,
        refresh_usage_pool_safe,
        should_push_remotes,
    )

    mark_live_usage_priority(int(user_id))
    await asyncio.sleep(0.15)
    async with _bg_usage_sem:
        try:
            async with _USAGE_PULL_GATE:
                before = 0
                try:
                    async with AsyncSessionLocal() as db:
                        row = (
                            await db.execute(
                                select(User.used_traffic, User.status).where(
                                    User.id == int(user_id)
                                )
                            )
                        ).one_or_none()
                        if row is None:
                            return
                        before = int(row[0] or 0)
                        st = row[1]
                        if st not in (
                            UserStatus.active,
                            UserStatus.on_hold,
                            UserStatus.limited,
                        ):
                            _dbg(
                                "H3",
                                "subscription.py:_bg_usage_pull",
                                "skip_status",
                                {
                                    "user_id": int(user_id),
                                    "status": str(st),
                                },
                            )
                            return
                except Exception:  # noqa: BLE001
                    pass
                _dbg(
                    "H2",
                    "subscription.py:_bg_usage_pull",
                    "before_refresh",
                    {
                        "user_id": int(user_id),
                        "used": before,
                    },
                )
                after, activated, changed, status_now = await asyncio.wait_for(
                    refresh_usage_pool_safe(
                        int(user_id), force=True, quick=True
                    ),
                    timeout=28.0,
                )
                if after < 0:
                    return
                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(User)
                        .where(User.id == int(user_id))
                        .options(
                            selectinload(User.panel_accounts).selectinload(
                                PanelAccount.panel
                            ),
                        )
                    )
                    user = result.scalar_one_or_none()
                    if user is None:
                        return
                    after = int(user.used_traffic or 0)
                    _dbg(
                        "H2",
                        "subscription.py:_bg_usage_pull",
                        "after_refresh",
                        {
                            "user_id": int(user_id),
                            "username": user.username,
                            "used_before": before,
                            "used_after": after,
                            "changed": bool(changed),
                            "activated": bool(activated),
                            "status": str(user.status),
                        },
                    )
                    blocked = user.status in (
                        UserStatus.limited,
                        UserStatus.expired,
                        UserStatus.disabled,
                    )
                    need_push = changed or blocked
                    if not need_push:
                        need_push = await should_push_remotes(
                            db,
                            user,
                            status_changed=False,
                            force=False,
                            shrink_shared_caps=True,
                        )
                    if need_push:
                        await asyncio.wait_for(
                            sync_modify_user(
                                db,
                                user,
                                skip_nginx_sync=True,
                                freeze_fast=True,
                            ),
                            timeout=14.0,
                        )
                        await db.commit()
        except Exception as exc:  # noqa: BLE001
            _dbg(
                "H2",
                "subscription.py:_bg_usage_pull",
                "pull_failed",
                {"user_id": int(user_id), "err": str(exc)[:200]},
            )
            logger.warning("bg usage pull user_id=%s: %s", user_id, exc)


async def _bg_sub_maintain_deferred(
    user_id: int, *, force: bool = False
) -> None:
    # Let the HTTP request finish + release its DB session first so concurrent
    # writers don't leave the request session in PendingRollback (500).
    await asyncio.sleep(0.25)
    await _bg_sub_maintain(user_id, force=force)


def _schedule_bg_maintain(user_id: int, *, force: bool = False) -> None:
    try:
        asyncio.create_task(
            _bg_sub_maintain_deferred(user_id, force=force),
            name=f"astra-sub-bg-{user_id}",
        )
    except RuntimeError:
        pass


def _schedule_bg_usage(user_id: int) -> None:
    """Debounced live usage pull after each real client sub refresh."""
    from app.services.orchestrator import mark_live_usage_priority

    uid = int(user_id)
    mark_live_usage_priority(uid)
    now = time.monotonic()
    last = float(_bg_usage_last.get(uid, 0.0) or 0.0)
    if now - last < _BG_USAGE_INTERVAL:
        # #region agent log
        _dbg(
            "H3",
            "subscription.py:_schedule_bg_usage",
            "debounced_skip",
            {"user_id": uid, "since": round(now - last, 2)},
        )
        # #endregion
        return
    _bg_usage_last[uid] = now
    # Cap map size
    if len(_bg_usage_last) > 8000:
        cutoff = now - 600.0
        for k, ts in list(_bg_usage_last.items()):
            if ts < cutoff:
                _bg_usage_last.pop(k, None)
    # #region agent log
    _dbg(
        "H3",
        "subscription.py:_schedule_bg_usage",
        "scheduled",
        {"user_id": uid},
    )
    # #endregion
    try:
        asyncio.create_task(
            _bg_usage_pull(uid),
            name=f"astra-sub-usage-{uid}",
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


def _sub_headers(user, *, body_rev: str | None = None) -> dict[str, str]:
    """Feed headers (PasarGuard-compatible).

    Aggressive no-cache so VPN apps (v2rayNG/Hiddify/…) actually replace the
    list on «Update». Edge/CDN often strips a lone Cache-Control — send several
    busting headers, a changing filename, and a revision query on the web URL.
    """
    upload = 0
    download = int(user.used_traffic or 0)
    total = int(user.data_limit or 0)
    expire = int(user.expire or 0)
    brand = get_brand_name()
    title_name = (getattr(user, "display_name", None) or "").strip() or user.username
    file_base = _safe_filename(user.username or "Astra")
    support = (get_link_support() or get_sub_base_url() or "").strip()
    sub = subscription_url(user)
    rev = (body_rev or "").strip() or str(int(time.time()))
    # Bust URL-keyed client caches without changing the token path
    sub_bust = sub
    if sub_bust:
        joiner = "&" if "?" in sub_bust else "?"
        sub_bust = f"{sub_bust}{joiner}rev={rev[:16]}"
    headers = {
        "profile-title": _header_base64(f"{brand}-{title_name}"),
        # Hours. Keep short so remark/tag changes reach clients quickly.
        "profile-update-interval": "1",
        "subscription-userinfo": (
            f"upload={upload}; download={download}; total={total}; expire={expire}"
        ),
        # No filename — some Android VPN clients treat disposition+filename as a
        # file download and never parse the Base64 subscription body.
        "content-disposition": "inline",
        "cache-control": "no-store, no-cache, must-revalidate, max-age=0, private",
        "pragma": "no-cache",
        "expires": "0",
        # Cloudflare / intermediate CDNs
        "cdn-cache-control": "no-store",
        "cloudflare-cdn-cache-control": "no-store",
        "surrogate-control": "no-store",
        "profile-web-page-url": sub_bust or sub,
        "x-astra-revision": rev[:32],
        # Range requests truncate Base64 → empty node list in v2rayNG/Hiddify.
        "accept-ranges": "none",
    }

    if support.startswith("http://") or support.startswith("https://"):
        headers["support-url"] = support
    # Do NOT attach Accept-CH / Critical-CH on the Base64 feed. Browsers handle
    # Critical-CH; OkHttp/v2rayNG often abort or retry wrongly → «آپدیت نشد»
    # while the same URL still opens fine in Safari/Chrome (HTML path).
    return headers


# VPN apps that should keep the ranked SUB_MAX (99) curated feed.
# Everything else (AstraPing, OkHttp/RN default UA, curl, empty) → full inventory.
_VPN_CLIENT_UA_MARKERS = (
    "v2ray",
    "clash",
    "sing-box",
    "singbox",
    "hiddify",
    "streisand",
    "shadowrocket",
    "quantumult",
    "surge",
    "loon",
    "stash",
    "nekobox",
    "nekoray",
    "v2box",
    "napsternet",
    "oneclick",
    "pharos",
    "fairvpn",
    "happ/",
    "foil",
    "karing",
    "throne",
    "sfa/",
    "sfm/",
    "sfi/",
)


def _wants_probe(request: Request) -> bool:
    """Explicit probe signal (query/header/AstraPing UA).

    Also used to skip analytics noise from admin/app refreshes.
    """
    for key in ("probe", "include_hidden", "all", "raw_all"):
        q = (request.query_params.get(key) or "").lower()
        if q in ("1", "true", "yes", "all"):
            return True
    h = (request.headers.get("x-astra-probe") or "").lower()
    if h in ("1", "true", "yes"):
        return True
    ua = (request.headers.get("user-agent") or "").lower()
    return "astraping" in ua


def _wants_curated_feed(request: Request) -> bool:
    """True → ranked SUB_MAX client feed (no hidden).

    React Native / OkHttp often strips custom User-Agent and may drop
    ``?probe=1`` behind some edges — those requests must still get the full
    inventory so AstraPing can clean/test every endpoint. Only well-known
    VPN client UAs stay on the curated 99-cap path.
    """
    if _wants_probe(request):
        return False
    # Force full feed
    for key in ("full", "uncapped"):
        q = (request.query_params.get(key) or "").lower()
        if q in ("1", "true", "yes", "all"):
            return False
    ua = (request.headers.get("user-agent") or "").lower().strip()
    if not ua:
        return False
    if "astraping" in ua or "okhttp" in ua:
        return False
    return any(m in ua for m in _VPN_CLIENT_UA_MARKERS)


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


def _looks_like_ipv6_host(link: str) -> bool:
    """True when the share URL host is an IPv6 literal (with or without brackets)."""
    raw = (link or "").strip()
    if not raw or "://" not in raw:
        return False
    if "@[" in raw:
        return True
    try:
        body = raw.split("://", 1)[1].split("#", 1)[0].split("?", 1)[0]
        hostport = body.split("@", 1)[-1]
        host = hostport.rsplit(":", 1)[0]
        return ":" in host and "." not in host
    except Exception:  # noqa: BLE001
        return False


def _feed_response(
    body: bytes,
    headers: dict[str, str],
    *,
    share_count: int | None = None,
) -> Response:
    """Plain uncompressed feed with Content-Length (no gzip, no chunked)."""
    import hashlib

    body_sha = hashlib.sha256(body).hexdigest() if body else "0"
    hdrs = {
        **headers,
        "content-length": str(len(body)),
        "accept-ranges": "none",
        # Strong validators — change whenever inventory/remarks change
        "etag": f'W/"{body_sha[:24]}"',
        "x-astra-body-sha256": body_sha[:32],
    }
    if share_count is not None:
        hdrs["x-astra-share-count"] = str(int(share_count))
        hdrs["x-astra-body-bytes"] = str(len(body))
    # Ensure no-cache even if caller forgot
    hdrs.setdefault(
        "cache-control", "no-store, no-cache, must-revalidate, max-age=0, private"
    )
    hdrs.setdefault("pragma", "no-cache")
    hdrs.setdefault("expires", "0")
    hdrs.pop("transfer-encoding", None)
    hdrs.pop("Transfer-Encoding", None)
    hdrs.pop("content-encoding", None)
    hdrs.pop("Content-Encoding", None)
    return Response(
        content=body,
        status_code=200,
        media_type="text/plain; charset=utf-8",
        headers=hdrs,
    )


async def _collect_feed_links(
    user,
    *,
    include_hidden: bool = False,
    db: AsyncSession | None = None,
    rank_operator: str | None = None,
) -> list[str]:
    """Assemble ordered/filtered share links from cached account data only."""
    return await collect_links(
        user,
        include_notices=False,
        include_hidden=include_hidden,
        db=db,
        live_rebuild=False,
        rank_operator=rank_operator,
    )


async def _collect_probe_panel_inventory(db: AsyncSession) -> list[str] | None:
    """Full Config-Order inventory for AstraPing — independent of pasted sub token.

    Returns one share URI per order fingerprint (same set as the admin
    «ترتیب کانفیگ‌ها» page, including globally-hidden rows). Any /sub/{token}
    with ``?probe=1`` can test the whole panel fleet even when that user
    only has a subset of groups.
    """
    try:
        from app.models.config_link_order import ConfigLinkOrder
        from app.services.config_orders import (
            _find_sample,
            _sample_fingerprints,
        )
    except Exception:  # noqa: BLE001
        logger.exception("probe inventory import failed")
        return None

    try:
        samples = await _sample_fingerprints(db)
    except Exception:  # noqa: BLE001
        logger.exception("probe inventory sample fingerprints failed")
        return None
    if not samples:
        return None

    ordered: list[str] = []
    seen: set[str] = set()

    def _push(raw: str | None) -> None:
        text = (raw or "").strip()
        if not text or text in seen:
            return
        seen.add(text)
        ordered.append(text)

    try:
        result = await db.execute(
            select(ConfigLinkOrder).order_by(
                ConfigLinkOrder.sort_order.asc(), ConfigLinkOrder.id.asc()
            )
        )
        rows = list(result.scalars().all())
    except Exception:  # noqa: BLE001
        rows = []

    for row in rows:
        sample = _find_sample(
            samples,
            str(row.host or ""),
            int(row.port or 0),
            str(row.match_host or ""),
        )
        if sample is None:
            continue
        _push(str(sample.get("sample_link") or ""))

    # Append any live sample endpoints not yet in saved order
    for sample in samples.values():
        _push(str(sample.get("sample_link") or ""))

    return ordered or None


def _rank_operator_from_request(request: Request) -> str | None:
    """Detect ISP from client IP for per-operator sub ordering.

    Uses Iran-edge ``X-Astra-Client-IP`` (real phone IP on Shatel/Rightel/…)
    so curated feed order matches the network used for Update/open.
    """
    try:
        from app.services.config_orders import to_ping_operator
        from app.services.iran_operators import classify_operator
        from app.services.user_analytics import extract_client_ip

        hdrs = {k: v for k, v in request.headers.items()}
        client_host = request.client.host if request.client else None
        ip = extract_client_ip(hdrs, client_host)
        op = to_ping_operator(classify_operator(ip))
        if op:
            logger.debug(
                "sub rank-op ip=%s… op=%s", (ip or "")[:24], op
            )
        return op
    except Exception:  # noqa: BLE001
        return None


async def _ensure_share_links(
    db: AsyncSession,
    token: str,
    user,
    *,
    timeout: float = 2.5,
    force: bool = False,
    include_hidden: bool = False,
):
    """Serve cached share links immediately; only briefly hydrate when empty.

    VPN clients timeout on long remotes — never block the HTTP path for a full
    multi-panel hydrate when we already have something to return. Background
    maintain fills gaps after the response.
    """
    # Fast path: any ready shares → serve now (even if some panels are incomplete).
    if not force and _user_has_share_links(user):
        try:
            links = await _collect_feed_links(
                user, include_hidden=include_hidden, db=db
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("sub collect-cached user=%s: %s", user.username, exc)
            links = []
        return user, links

    # New user / sub-URL-only accounts: expand via pool-safe hydrate (own sessions).
    try:
        from app.services.orchestrator import hydrate_share_links_pool_safe

        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
        n = await asyncio.wait_for(
            hydrate_share_links_pool_safe(int(user.id)),
            timeout=max(8.0, float(timeout)),
        )
        if n:
            logger.info("sub hydrated user=%s accounts=%s", user.username, n)
        user = await load_user_by_token(db, token) or user
    except Exception as exc:  # noqa: BLE001
        logger.warning("sub hydrate user=%s: %s", getattr(user, "username", "?"), exc)
        try:
            user = await load_user_by_token(db, token) or user
        except Exception:  # noqa: BLE001
            pass
    links: list[str] = []
    try:
        links = await _collect_feed_links(
            user, include_hidden=include_hidden, db=db
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("sub collect user=%s: %s", getattr(user, "username", "?"), exc)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        try:
            user = await load_user_by_token(db, token) or user
            links = await _collect_feed_links(
                user, include_hidden=include_hidden, db=db
            )
        except Exception as exc2:  # noqa: BLE001
            logger.warning("sub collect retry user=%s: %s", getattr(user, "username", "?"), exc2)
            links = []
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
        activated = await activate_on_hold_if_used(db, user)
        status_changed = bool(activated) or await enforce_user_quota(db, user)
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


def _fallback_sub_html(username: str = "") -> str:
    safe = (username or "کاربر").replace("<", "").replace(">", "")[:64]
    return (
        "<!DOCTYPE html><html lang='fa' dir='rtl'><head><meta charset='utf-8'/>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'/>"
        "<title>اشتراک</title>"
        "<style>body{margin:0;min-height:100vh;display:grid;place-items:center;"
        "font-family:system-ui,sans-serif;background:#07141e;color:#ecfaf6}"
        "main{text-align:center;padding:24px}h1{font-size:1.1rem;margin:0 0 8px}"
        "p{margin:0;color:#7fa39a;font-size:.9rem}</style></head>"
        f"<body><main><h1>اشتراک {safe}</h1>"
        "<p>در حال آماده‌سازی کانفیگ‌ها — چند ثانیه بعد دوباره باز کنید.</p>"
        "</main></body></html>"
    )


async def _render_info_page_body(
    db: AsyncSession,
    token: str,
    user,
    *,
    rank_operator: str | None = None,
) -> HTMLResponse:
    """Build the browser info page from cached data only (no remote pull)."""
    await ensure_loaded(db)
    # Never block HTML on hydrate/usage — that caused «در حال آماده‌سازی».
    try:
        _schedule_bg_maintain(int(user.id))
    except Exception:  # noqa: BLE001
        pass
    try:
        _schedule_bg_usage(int(user.id))
    except Exception:  # noqa: BLE001
        pass

    try:
        # Same ISP order as the VPN Base64 feed (Rightel/Shatel/…).
        configs = await collect_links_detailed(
            user,
            include_notices=True,
            db=db,
            live_rebuild=False,
            rank_operator=rank_operator,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("sub detailed user=%s: %s", user.username, exc)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        configs = []
    # Skip usage_breakdown SQL on hot HTML path — page ignores panels_usage anyway.
    panels_usage: list = []
    try:
        device_limit = await device_limit_for_user(db, user)
    except Exception:  # noqa: BLE001
        device_limit = 0
    status = user.status.value if hasattr(user.status, "value") else str(user.status)
    show_name = (getattr(user, "display_name", None) or "").strip() or user.username
    # Without first promo, hide brand on the public sub page (privacy / no-ad users).
    page_brand = (
        get_brand_name()
        if bool(getattr(user, "include_promo_first", True))
        else "sub links"
    )
    html = render_subscription_page(
        panel_name=page_brand,
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
        rank_operator=rank_operator,
    )
    html_headers = {
        "cache-control": "no-store, no-cache, must-revalidate, max-age=0, private",
        "pragma": "no-cache",
        "expires": "0",
        "cdn-cache-control": "no-store",
        "cloudflare-cdn-cache-control": "no-store",
        "surrogate-control": "no-store",
        "content-disposition": "inline",
        "x-astra-rank-op": (rank_operator or "global")[:32],
        "x-astra-html-rev": "312-cfg-snippet",
    }
    try:
        from app.services.client_ua import ACCEPT_CH_HEADERS

        # Client Hints only on HTML — never on the Base64 VPN feed.
        html_headers.update(ACCEPT_CH_HEADERS)
    except Exception:  # noqa: BLE001
        pass
    return HTMLResponse(html, headers=html_headers)


async def _render_info_page(
    db: AsyncSession,
    token: str,
    user,
    *,
    rank_operator: str | None = None,
) -> HTMLResponse:
    """HTML info page — hard wall-clock cap so Iran nginx never 504s the browser."""
    name = getattr(user, "username", "") or ""
    try:
        # Fast answer only — usage/heal stay in background tasks.
        return await asyncio.wait_for(
            _render_info_page_body(
                db, token, user, rank_operator=rank_operator
            ),
            timeout=3.5,
        )
    except asyncio.TimeoutError:
        logger.warning("sub html deadline user=%s — fallback page", name)
        try:
            _schedule_bg_maintain(int(user.id))
            _schedule_bg_usage(int(user.id))
        except Exception:  # noqa: BLE001
            pass
        return HTMLResponse(
            _fallback_sub_html(name),
            status_code=200,
            headers={"cache-control": "no-store"},
        )
    except BaseException as exc:  # noqa: BLE001
        if _is_cancel(exc):
            raise
        logger.exception("sub html failed user=%s", name)
        await _safe_rollback(db)
        return HTMLResponse(
            _fallback_sub_html(name),
            status_code=200,
            headers={"cache-control": "no-store"},
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

    Must never return 500 through the Iran reverse tunnel — clients show
    "Internal Server Error" and abandon the update.
    """
    try:
        user = await load_user_by_token(db, token)
    except BaseException as exc:  # noqa: BLE001
        if _is_cancel(exc):
            raise
        logger.exception("sub load token=%s…", (token or "")[:8])
        await _safe_rollback(db)
        return PlainTextResponse("Temporarily unavailable", status_code=503)

    if not user:
        return PlainTextResponse("Not found", status_code=404)

    # Snapshot scalars before any rollback/expire — exception paths must not
    # lazy-load ORM attrs (MissingGreenlet → HTTP 500 → VPN «آپدیت نشد»).
    uname_safe = str(getattr(user, "username", None) or "?")
    uid_safe = int(user.id)

    # #region agent log
    _t0 = time.monotonic()
    _dbg(
        "H7",
        "subscription.py:get_subscription",
        "enter",
        {
            "user_id": uid_safe,
            "username": uname_safe,
            "status": str(user.status),
            "used": int(user.used_traffic or 0),
            "limit": int(user.data_limit or 0),
            "token8": (token or "")[:8],
            "ua": (request.headers.get("user-agent") or "")[:60],
            "host": (request.headers.get("host") or "")[:40],
        },
    )
    # #endregion

    # Analytics: log sub opens (HTML + feed) so quota-watch prioritizes them
    is_probe = _wants_probe(request)
    is_html = _wants_html_page(request)
    try:
        if not is_probe:
            from app.services.user_analytics import record_sub_fetch

            hdrs = {k: v for k, v in request.headers.items()}
            client_host = request.client.host if request.client else None
            asyncio.create_task(
                record_sub_fetch(
                    user_id=int(user.id),
                    headers=hdrs,
                    client_host=client_host,
                    is_probe=False,
                ),
                name=f"astra-sub-fetch-{int(user.id)}",
            )
    except Exception:  # noqa: BLE001
        pass

    # Live usage: every real client refresh pulls remotes in the background so
    # quota / «مصرف» / analytics stay accurate without blocking the feed.
    if not is_probe:
        try:
            _schedule_bg_usage(int(user.id))
        except Exception:  # noqa: BLE001
            pass

    try:
        if is_html:
            # Browser page must use the same ISP order as Update in the app.
            return await _render_info_page(
                db,
                token,
                user,
                rank_operator=_rank_operator_from_request(request),
            )

        from app.services.site_settings import ensure_loaded

        await ensure_loaded(db)

        # Curated 99-cap only for known VPN apps. AstraPing / OkHttp / probe
        # get the full inventory (incl. globally-hidden) for cleaning/tests.
        curated = _wants_curated_feed(request)
        include_hidden = not curated

        # Detect ISP from real client IP (X-Astra-Client-IP / CF / XFF).
        # Shatel / Rightel / Irancell / … → that ISP's ping-built order.
        rank_operator = (
            _rank_operator_from_request(request) if curated else None
        )
        def _headers_for(body: bytes) -> dict[str, str]:
            import hashlib

            digest = hashlib.sha256(body or b"").hexdigest()[:12]
            # time suffix → every Update gets new Content-Disposition / web URL
            # so URL-keyed HTTP caches on the phone cannot reuse a stale body.
            hdrs = _sub_headers(
                user, body_rev=f"{digest}-{int(time.time())}"
            )
            # Lets us verify which ISP order the client received.
            hdrs["x-astra-rank-op"] = (rank_operator or "global")[:32]
            return hdrs

        # limited/expired: still return updateable feed with «تموم/منقضی» remarks.
        # disabled: notice-only for VPN apps; AstraPing probe still gets full inventory.
        if user.status == UserStatus.disabled and curated:
            from app.services.link_remark import build_notice_link, build_status_row
            from app.services.site_settings import (
                get_status_line_template,
                get_status_panel_label,
            )

            st_row = build_status_row(
                username=(user.username or "user"),
                used=int(user.used_traffic or 0),
                limit=int(user.data_limit or 0),
                expire=user.expire,
                template=get_status_line_template(),
                panel_label=get_status_panel_label(),
                user_status="disabled",
            )
            notice = build_notice_link("⛔️ اشتراک غیرفعال شده")
            body = _encode_feed(
                [st_row["link"]] + ([notice] if notice else [])
            )
            return _feed_response(body, _headers_for(body))

        # Recover missing group×panel remotes after panel outages (non-blocking)
        try:
            complete = await _user_sub_links_complete(db, user)
        except Exception:  # noqa: BLE001
            complete = True
        if not complete:
            _schedule_bg_maintain(int(user.id))

        # Incomplete coverage: bg pool-safe heal fills gaps. Avoid long sync
        # hydrate on the request session (held checkout → QueuePool starve).
        # Only do a short pool-safe hydrate when we have nothing to serve yet.
        if not complete:
            _schedule_bg_maintain(int(user.id), force=True)
            if not _user_has_share_links(user):
                try:
                    await db.commit()
                except Exception:  # noqa: BLE001
                    try:
                        await db.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    from app.services.orchestrator import hydrate_share_links_pool_safe

                    n = await asyncio.wait_for(
                        hydrate_share_links_pool_safe(int(user.id)),
                        timeout=6.0 if curated else 7.0,
                    )
                    if n:
                        invalidate_user_feed_cache(int(user.id))
                        user = await load_user_by_token(db, token) or user
                        logger.info(
                            "sub quick-hydrate pool-safe user=%s accounts=%s curated=%s",
                            getattr(user, "username", "?"),
                            n,
                            int(bool(curated)),
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "sub quick-hydrate user=%s: %s",
                        uname_safe,
                        exc,
                    )
                    try:
                        user = await load_user_by_token(db, token) or user
                    except Exception:  # noqa: BLE001
                        pass

        # Never serve stale active cache after flip to limited/expired
        blocked = user.status in (UserStatus.limited, UserStatus.expired)
        if blocked:
            invalidate_user_feed_cache(int(user.id))
        # Curated VPN feeds: never serve in-process cache on Update — phones
        # already fight edge/CDN caches; a 12s sticky short body made «آپدیت»
        # look broken. Probe still rebuilds every time (include_hidden).
        cached = None

        # Tunnel nginx 502s if this handler is slow or cancelled. DB links only.
        # Full/probe inventory is heavier — allow more time so AstraPing gets all.
        # AstraPing: always the Config-Order sample inventory (all panels), not
        # only the pasted user's groups — so Update matches «۲۹۰ کانفیگ یکتا».
        links: list[str] = []
        try:
            if include_hidden:
                inv = await asyncio.wait_for(
                    _collect_probe_panel_inventory(db),
                    timeout=25.0,
                )
                if inv:
                    links = inv
                    logger.info(
                        "sub probe inventory user=%s configs=%s (panel sample)",
                        getattr(user, "username", "?"),
                        len(links),
                    )
            if not links:
                # 5s was too tight under group-resync DB load → many VPN
                # clients got a soft-empty body even when links existed.
                links = await asyncio.wait_for(
                    _collect_feed_links(
                        user,
                        include_hidden=include_hidden,
                        db=db,
                        rank_operator=rank_operator,
                    ),
                    timeout=30.0 if include_hidden else 18.0,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("sub collect user=%s: %s", user.username, exc)
            links = []

        # Older v2rayNG builds choke on IPv6 literals in the whole sub import.
        if curated:
            links = [
                x
                for x in links
                if x and "@[" not in x and not _looks_like_ipv6_host(x)
            ]

        from app.adapters.http_utils import is_share_link as _isl

        real_n = sum(
            1
            for x in links
            if _isl(x) and "127.0.0.1" not in (x or "")
        )

        # Empty / sparse feed → heal missing remotes + hydrate, then re-collect.
        # Covers: panel removed then re-added without full apply, timeout mid-
        # collect, and claim/link gaps after group resync.
        if not include_hidden and real_n < 55:
            _schedule_bg_maintain(int(user.id), force=True)
            try:
                from app.services.orchestrator import (
                    heal_user_coverage_pool_safe,
                    hydrate_share_links_pool_safe,
                )

                try:
                    await db.commit()
                except Exception:  # noqa: BLE001
                    pass
                if real_n <= 0:
                    try:
                        await asyncio.wait_for(
                            heal_user_coverage_pool_safe(int(user.id)),
                            timeout=12.0,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "sub empty-heal user=%s: %s", uname_safe, exc
                        )
                n2 = await asyncio.wait_for(
                    hydrate_share_links_pool_safe(int(user.id)),
                    timeout=8.0 if real_n <= 0 else 5.0,
                )
                if n2 or real_n <= 0:
                    invalidate_user_feed_cache(int(user.id))
                    user = await load_user_by_token(db, token) or user
                    links = await asyncio.wait_for(
                        _collect_feed_links(
                            user,
                            include_hidden=False,
                            db=db,
                            rank_operator=rank_operator,
                        ),
                        timeout=18.0,
                    )
                    if curated:
                        links = [
                            x
                            for x in links
                            if x and "@[" not in x and not _looks_like_ipv6_host(x)
                        ]
                    real_n = sum(
                        1
                        for x in links
                        if _isl(x) and "127.0.0.1" not in (x or "")
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "sub sparse-hydrate user=%s: %s", uname_safe, exc
                )
                try:
                    await db.rollback()
                except Exception:  # noqa: BLE001
                    pass

        body = _encode_feed(links)
        if not body or real_n <= 0:
            logger.warning(
                "sub returning empty body user=%s token=%s… real=%s",
                user.username,
                token[:8],
                real_n,
            )

        # #region agent log
        _dbg(
            "H7",
            "subscription.py:get_subscription",
            "feed_done",
            {
                "user_id": int(user.id),
                "username": user.username,
                "dt_ms": int((time.monotonic() - _t0) * 1000),
                "links": len(links),
                "real_n": real_n,
                "body_len": len(body),
                "complete": bool(complete),
                "used": int(user.used_traffic or 0),
                "rank_op": rank_operator or "global",
            },
        )
        # #endregion
        return _feed_response(
            body, _headers_for(body), share_count=real_n
        )
    except BaseException as exc:  # noqa: BLE001
        if _is_cancel(exc):
            raise
        logger.exception("sub feed failed user=%s", uname_safe)
        await _safe_rollback(db)
        # Soft 200 empty feed — better than Internal Server Error on tunnel
        try:
            user = await load_user_by_token(db, token) or user
            headers = _sub_headers(user)
        except Exception:  # noqa: BLE001
            headers = {"cache-control": "no-store"}
        return _feed_response(b"", headers)


@router.get("/sub/{token}/usage")
async def subscription_usage(token: str, db: AsyncSession = Depends(get_db)):
    """Lightweight live counters for the HTML page (poll every few seconds).

    Schedules a background panel pull so the next poll reflects remotes × coeffs.
    Never blocks on remote HTTP — keeps Iran tunnel under the 504 wall.
    """
    from fastapi.responses import JSONResponse

    try:
        user = await load_user_by_token(db, token)
    except BaseException as exc:  # noqa: BLE001
        if _is_cancel(exc):
            raise
        await _safe_rollback(db)
        return JSONResponse({"error": "unavailable"}, status_code=503)
    if not user:
        return JSONResponse({"error": "not_found"}, status_code=404)

    try:
        _schedule_bg_usage(int(user.id))
    except Exception:  # noqa: BLE001
        pass

    status = user.status.value if hasattr(user.status, "value") else str(user.status)
    used = int(user.used_traffic or 0)
    limit = int(user.data_limit or 0)
    return JSONResponse(
        {
            "username": user.username,
            "status": status,
            "used": used,
            "limit": limit,
            "remain": None if limit <= 0 else max(0, limit - used),
            "expire": int(user.expire or 0) or None,
        },
        headers={"cache-control": "no-store"},
    )


@router.get("/sub/{token}/info", response_class=HTMLResponse)
async def subscription_page(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    try:
        user = await load_user_by_token(db, token)
    except BaseException as exc:  # noqa: BLE001
        if _is_cancel(exc):
            raise
        logger.exception("sub info load token=%s…", (token or "")[:8])
        await _safe_rollback(db)
        return HTMLResponse(_fallback_sub_html(), status_code=200)
    if not user:
        return HTMLResponse("<h1>Not found</h1>", status_code=404)
    return await _render_info_page(
        db,
        token,
        user,
        rank_operator=_rank_operator_from_request(request),
    )
