import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal, get_db
from app.models.user import PanelAccount, PanelAccountStatus, User, UserStatus
from app.services.orchestrator import (
    activate_on_hold_if_used,
    collect_links,
    collect_links_detailed,
    device_limit_for_user,
    enforce_user_quota,
    hydrate_share_links,
    refresh_usage,
    sync_modify_user,
    sync_modify_user_background,
)
from app.services.site_settings import (
    ensure_loaded,
    get_brand_name,
    get_link_support,
    get_public_links,
    get_sub_base_url,
)
from app.services.sub_page import render_subscription_page
from app.services.users import (
    load_user_by_token,
    load_user_by_token_light,
    subscription_url,
)

router = APIRouter(tags=["Subscription"])
logger = logging.getLogger("astra.sub")


# #region agent log
def _dbg(hypothesis_id: str, location: str, message: str, data: dict | None = None) -> None:
    """Hot-path debug — logger only (sync disk I/O was starving /sub Update)."""
    try:
        logger.debug("DBG941288 %s %s %s", hypothesis_id, message, data or {})
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
_BG_MIN_INTERVAL = 45.0
# Parallel heal/hydrate — keep low so QueuePool keeps headroom for /sub.
# (Was 6; each task used to hold a DB checkout across remote HTTP.)
_bg_heavy = asyncio.Semaphore(2)
_BG_MAINTAIN_GATE = asyncio.Semaphore(2)

# Live usage pull on sub refresh — must stay accurate (quota / money).
_bg_usage_last: dict[int, float] = {}
# Sub-triggered pulls; fleet/hot run every ~5 min for the rest.
# 5s single-flight so concurrent /sub Update taps coalesce (was 15s).
_BG_USAGE_INTERVAL = 5.0
# Parallel /sub usage pulls — keep under DB pool headroom.
_bg_usage_sem = asyncio.Semaphore(2)

# Tiny in-process feed cache — VPN clients often refresh in bursts.
# Incomplete coverage also caches (shorter TTL) so Iran-edge clients don't
# wait on a full rebuild every tap of «Update» while bg heal catches up.
# Cache key: (user_id, include_hidden, op_bucket) — op_bucket separates ISP orders
_feed_cache: dict[tuple[int, bool, str], tuple[float, bytes, bool]] = {}
# Longer TTL = more instant Update hits (apps still send no-cache; server is warm).
# V2Box / Happ / v2rayNG hammer Update; keep warm bodies for minutes.
_FEED_CACHE_TTL = 300.0
# Emergency/raw bodies must expire fast so group config_tag names replace them
_FEED_CACHE_TTL_PARTIAL = 25.0
_FEED_CACHE_MAX = 4000

# Token→body cache: serve Update with ZERO DB checkout (survives pool starve).
# meta: user_id, username, display_name, used_traffic, data_limit, expire, status
_token_feed_cache: dict[tuple[str, str], tuple[float, bytes, dict]] = {}
_TOKEN_FEED_MAX = 6000
# Shared across uvicorn workers — memory miss still hits disk warm body.
_TOKEN_DISK_DIR = Path("/tmp/astra_sub_tok")
_TOKEN_DISK_TTL = 360.0

# Background ranked rebuild after emergency-first miss (debounced).
_bg_rank_last: dict[int, float] = {}
_BG_RANK_INTERVAL = 8.0
_bg_rank_sem = asyncio.Semaphore(4)


def _op_bucket(rank_operator: str | None) -> str:
    from app.services.config_orders import to_ping_operator

    return to_ping_operator(rank_operator) or "global"


def _feed_cache_get(
    user_id: int, include_hidden: bool, rank_operator: str | None = None
) -> bytes | None:
    entry = _feed_cache_entry(user_id, include_hidden, rank_operator)
    return entry[0] if entry else None


def _feed_cache_entry(
    user_id: int, include_hidden: bool, rank_operator: str | None = None
) -> tuple[bytes, bool] | None:
    """Return ``(body, complete)`` — complete=False means emergency/raw names."""
    key = (int(user_id), bool(include_hidden), _op_bucket(rank_operator))
    hit = _feed_cache.get(key)
    if not hit:
        return None
    ts, body, complete = hit
    ttl = _FEED_CACHE_TTL if complete else _FEED_CACHE_TTL_PARTIAL
    if time.monotonic() - ts > ttl:
        _feed_cache.pop(key, None)
        return None
    # Never serve a blank Base64 body from cache (apps show 0 nodes).
    if not body:
        _feed_cache.pop(key, None)
        return None
    return body, bool(complete)


def _feed_cache_put(
    user_id: int,
    include_hidden: bool,
    body: bytes,
    *,
    complete: bool = True,
    rank_operator: str | None = None,
) -> None:
    if not body:
        return
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


def _token_disk_key(token: str, rank_operator: str | None = None) -> str:
    raw = f"{(token or '').strip()}|{_op_bucket(rank_operator)}"
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:40]


def _token_disk_path(token: str, rank_operator: str | None = None) -> Path:
    return _TOKEN_DISK_DIR / f"{_token_disk_key(token, rank_operator)}.bin"


def _token_disk_get(
    token: str, rank_operator: str | None = None
) -> tuple[bytes, dict] | None:
    try:
        path = _token_disk_path(token, rank_operator)
        meta_path = path.with_suffix(".meta")
        if not path.is_file() or not meta_path.is_file():
            return None
        age = time.time() - path.stat().st_mtime
        if age > _TOKEN_DISK_TTL:
            try:
                path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            return None
        complete = bool(meta.get("complete", True))
        ttl = _FEED_CACHE_TTL if complete else _FEED_CACHE_TTL_PARTIAL
        if age > ttl:
            return None
        body = path.read_bytes()
        if not body:
            return None
        return body, meta
    except Exception:  # noqa: BLE001
        return None


def _token_disk_put(
    token: str,
    body: bytes,
    meta: dict,
    *,
    rank_operator: str | None = None,
) -> None:
    if not body:
        return
    try:
        _TOKEN_DISK_DIR.mkdir(parents=True, exist_ok=True)
        path = _token_disk_path(token, rank_operator)
        meta_path = path.with_suffix(".meta")
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(body)
        tmp.replace(path)
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass


def _token_disk_clear() -> None:
    try:
        if not _TOKEN_DISK_DIR.is_dir():
            return
        for p in _TOKEN_DISK_DIR.iterdir():
            try:
                p.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


def _token_feed_get(
    token: str, rank_operator: str | None = None
) -> tuple[bytes, dict] | None:
    key = ((token or "").strip(), _op_bucket(rank_operator))
    hit = _token_feed_cache.get(key)
    if hit:
        ts, body, meta = hit
        complete = bool(meta.get("complete", True))
        ttl = _FEED_CACHE_TTL if complete else _FEED_CACHE_TTL_PARTIAL
        if time.monotonic() - ts > ttl or not body:
            _token_feed_cache.pop(key, None)
        else:
            return body, meta
    # Shared Redis L2 — hits across uvicorn workers without DB.
    try:
        from app.core.redis_cache import sub_cache_get

        rbody = sub_cache_get(token, _op_bucket(rank_operator))
        if rbody:
            meta = {"complete": True, "from": "redis"}
            _token_feed_cache[key] = (time.monotonic(), rbody, meta)
            return rbody, meta
    except Exception:  # noqa: BLE001
        pass
    disk = _token_disk_get(token, rank_operator)
    if disk:
        body, meta = disk
        if not body:
            return None
        # Warm this worker's memory so the next Update is pure RAM.
        _token_feed_cache[key] = (time.monotonic(), body, dict(meta))
        return body, meta
    return None


def _token_feed_put(
    token: str,
    body: bytes,
    meta: dict,
    *,
    rank_operator: str | None = None,
) -> None:
    if not body:
        return
    if len(_token_feed_cache) >= _TOKEN_FEED_MAX:
        for k, _ in sorted(_token_feed_cache.items(), key=lambda kv: kv[1][0])[
            : max(1, _TOKEN_FEED_MAX // 10)
        ]:
            _token_feed_cache.pop(k, None)
    payload = dict(meta)
    _token_feed_cache[((token or "").strip(), _op_bucket(rank_operator))] = (
        time.monotonic(),
        body,
        payload,
    )
    _token_disk_put(token, body, payload, rank_operator=rank_operator)
    # Redis L2 — only complete feeds (partial/emergency stay short-lived local).
    if bool(payload.get("complete", True)):
        try:
            from app.core.redis_cache import sub_cache_set

            uid = payload.get("user_id")
            sub_cache_set(
                token,
                body,
                op_bucket=_op_bucket(rank_operator),
                user_id=int(uid) if uid is not None else None,
            )
        except Exception:  # noqa: BLE001
            pass


def invalidate_user_feed_cache(*user_ids: int) -> None:
    """Drop cached sub bodies after membership / remote changes."""
    if not user_ids:
        return
    want = {int(u) for u in user_ids if u is not None}
    for key in list(_feed_cache.keys()):
        if key[0] in want:
            _feed_cache.pop(key, None)
    for key, (_ts, _body, meta) in list(_token_feed_cache.items()):
        try:
            if int(meta.get("user_id") or 0) in want:
                _token_feed_cache.pop(key, None)
        except Exception:
            _token_feed_cache.pop(key, None)
    # Only purge matching disk entries — full wipe caused fleet-wide cold misses
    # whenever one user was hydrated/healed (V2Box Update stampede).
    try:
        if _TOKEN_DISK_DIR.is_dir():
            for meta_path in _TOKEN_DISK_DIR.glob("*.meta"):
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    if int(meta.get("user_id") or 0) in want:
                        meta_path.unlink(missing_ok=True)
                        meta_path.with_suffix(".bin").unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.core.redis_cache import sub_cache_invalidate_users

        sub_cache_invalidate_users(*sorted(want))
    except Exception:  # noqa: BLE001
        pass


def clear_feed_cache() -> None:
    """Drop all feed bodies (e.g. after ISP order maps refresh)."""
    _feed_cache.clear()
    _token_feed_cache.clear()
    _token_disk_clear()


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
            blocked_early = False
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
                # Flip expire before any heal — never provision dead subs.
                try:
                    await activate_on_hold_if_used(db, user)
                    await enforce_user_quota(db, user)
                except Exception:  # noqa: BLE001
                    pass
                blocked_early = user.status in (
                    UserStatus.limited,
                    UserStatus.expired,
                    UserStatus.disabled,
                )
                if not blocked_early:
                    complete = await _user_sub_links_complete(db, user)
                await db.commit()
            # Session closed before remote heal/hydrate.

            # Expired/limited: freeze only — heal must not create fresh remotes.
            if not blocked_early and not complete:
                try:
                    await asyncio.wait_for(
                        heal_user_coverage_pool_safe(
                            int(user_id), wait=14.0, include_empty_links=True
                        ),
                        timeout=25.0,
                    )
                except Exception as exc:  # noqa: BLE001
                    if "connection is closed" not in str(exc).lower():
                        logger.warning("bg heal user_id=%s: %s", user_id, exc)
                try:
                    await asyncio.wait_for(
                        hydrate_share_links_pool_safe(int(user_id)), timeout=14.0
                    )
                    invalidate_user_feed_cache(int(user_id))
                except Exception as exc:  # noqa: BLE001
                    if "connection is closed" not in str(exc).lower():
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
                    if blocked or blocked_early:
                        await db.commit()
                        from app.services.orchestrator import kick_freeze_user

                        await kick_freeze_user(int(user_id), remote_timeout=10.0)
                        invalidate_user_feed_cache(int(user_id))
                    elif changed:
                        await db.commit()
                        # Fire-and-forget — never hold this session across remotes.
                        try:
                            asyncio.create_task(
                                sync_modify_user_background(int(user_id)),
                                name=f"astra-sub-sync-{int(user_id)}",
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        invalidate_user_feed_cache(int(user_id))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("bg quota user_id=%s: %s", user_id, exc)
                    try:
                        await db.rollback()
                    except Exception:  # noqa: BLE001
                        pass
    except Exception:  # noqa: BLE001
        logger.exception("bg sub maintain user_id=%s failed", user_id)


async def _bg_usage_pull(user_id: int) -> None:
    """Advance quota from meters after /sub — never reuse the request session.

    Always opens its own ``AsyncSessionLocal`` sessions and never holds a DB
    connection across remote HTTP (that caused InterfaceError after the pool
    guardian / idle-in-txn timeout closed the backend).

    When a Worker is healthy it owns remote meters — this task only recounts
    ``users.used_traffic`` from ``panel_accounts`` already written by usage-report.
    """
    from app.services.orchestrator import (
        _USAGE_PULL_GATE,
        mark_live_usage_priority,
        recount_user_usage_pool_safe,
        refresh_usage_pool_safe,
        sync_modify_user_background,
    )

    mark_live_usage_priority(int(user_id))
    # Let the HTTP request finish and release its session first.
    await asyncio.sleep(0.2)
    async with _bg_usage_sem:
        try:
            async with _USAGE_PULL_GATE:
                st = None
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
                        st = row[1]
                        if st not in (
                            UserStatus.active,
                            UserStatus.on_hold,
                            UserStatus.limited,
                        ):
                            return
                except Exception:  # noqa: BLE001
                    return

                worker_ok = False
                try:
                    from app.services.worker_client import any_worker_healthy

                    worker_ok = bool(any_worker_healthy())
                except Exception:  # noqa: BLE001
                    worker_ok = False

                # Worker path: enqueue priority probe, pull via worker (cache-first
                # / live), then recount. Never recount-only — that left /sub users
                # stuck on stale panel_accounts while remotes had real traffic.
                if worker_ok:
                    try:
                        from app.services import usage_priority as uprio

                        await uprio.enqueue_user_remotes(int(user_id), ttl=900.0)
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        await asyncio.wait_for(
                            refresh_usage_pool_safe(
                                int(user_id), force=True, quick=True
                            ),
                            timeout=12.0,
                        )
                    except asyncio.TimeoutError:
                        try:
                            await recount_user_usage_pool_safe(int(user_id))
                        except Exception:  # noqa: BLE001
                            pass
                    except Exception as exc:  # noqa: BLE001
                        if "connection is closed" not in str(exc).lower():
                            logger.warning(
                                "bg usage refresh user_id=%s: %s", user_id, exc
                            )
                        try:
                            await recount_user_usage_pool_safe(int(user_id))
                        except Exception:  # noqa: BLE001
                            pass
                    # Let priority-probe reports land, then recount once more.
                    await asyncio.sleep(2.0)
                    try:
                        await recount_user_usage_pool_safe(int(user_id))
                    except Exception:  # noqa: BLE001
                        pass
                    # Quota flip / freeze without holding a request-tied session.
                    if st == UserStatus.limited:
                        try:
                            from app.services.orchestrator import kick_freeze_user
                            from app.services.worker_commands import (
                                kill_on_cooldown,
                            )

                            # Re-cut when cooldown elapsed — earlier kills may
                            # have been trimmed from the Redis command queue.
                            await kick_freeze_user(
                                int(user_id),
                                remote_timeout=8.0,
                                force=not kill_on_cooldown(int(user_id)),
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    return

                # No worker: light remote refresh (own sessions inside pool-safe).
                try:
                    after, _activated, changed, _status = await asyncio.wait_for(
                        refresh_usage_pool_safe(
                            int(user_id), force=True, quick=True
                        ),
                        timeout=16.0,
                    )
                except asyncio.TimeoutError:
                    after = await recount_user_usage_pool_safe(int(user_id))
                    changed = False
                    logger.info(
                        "bg usage pull timeout user_id=%s — kept recount=%s",
                        user_id,
                        after,
                    )
                except Exception as exc:  # noqa: BLE001
                    if "connection is closed" in str(exc).lower():
                        return
                    logger.warning("bg usage pull user_id=%s: %s", user_id, exc)
                    return
                if after < 0:
                    return
                if changed or st == UserStatus.limited:
                    # Fire-and-forget — opens its own sessions; never pass ours.
                    try:
                        asyncio.create_task(
                            sync_modify_user_background(int(user_id)),
                            name=f"astra-sub-sync-{int(user_id)}",
                        )
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001
            if "connection is closed" in str(exc).lower():
                return
            logger.warning("bg usage pull user_id=%s: %s", user_id, exc)


async def _bg_sub_maintain_deferred(
    user_id: int, *, force: bool = False
) -> None:
    # Let the HTTP request finish + release its DB session first so concurrent
    # writers don't leave the request session in PendingRollback (500).
    await asyncio.sleep(0.05 if force else 0.12)
    await _bg_sub_maintain(user_id, force=force)


def _schedule_bg_maintain(user_id: int, *, force: bool = False) -> None:
    async def _gated() -> None:
        async with _BG_MAINTAIN_GATE:
            await _bg_sub_maintain_deferred(user_id, force=force)

    try:
        asyncio.create_task(
            _gated(),
            name=f"astra-sub-bg-{user_id}",
        )
    except RuntimeError:
        pass


def _schedule_bg_usage(user_id: int) -> None:
    """Debounced live usage pull after each real client sub refresh."""
    from app.services.orchestrator import mark_live_usage_priority

    uid = int(user_id)
    mark_live_usage_priority(uid)
    try:
        from app.services.usage_tiers import mark_sub_touch

        mark_sub_touch(uid)
    except Exception:  # noqa: BLE001
        pass
    # Always push exact remotes into Worker priority-probe (cheap, deduped)
    # so meters refresh within ~3s even when the heavier pull is debounced.
    try:
        from app.services import usage_priority as uprio

        asyncio.create_task(
            uprio.enqueue_user_remotes(uid, ttl=900.0),
            name=f"astra-prio-enqueue-{uid}",
        )
    except Exception:  # noqa: BLE001
        pass
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

    Default is curated for ALL clients (including OkHttp / empty UA). The old
    «unknown UA → full probe inventory» path stampeded origin CPU and made
    every app's «Update» time out. Full inventory only for AstraPing /
    ``?probe=1`` / ``?full=1``.
    """
    if _wants_probe(request):
        return False
    # Force full feed (admin / AstraPing cleaning)
    for key in ("full", "uncapped"):
        q = (request.query_params.get(key) or "").lower()
        if q in ("1", "true", "yes", "all"):
            return False
    ua = (request.headers.get("user-agent") or "").lower().strip()
    if "astraping" in ua:
        return False
    return True


def _wants_html_page(request: Request) -> bool:
    """
    Browser *navigation* → redirect to ``/sub/{token}/info``.

    ``/sub/{token}`` itself is ALWAYS the Base64 feed for VPN apps. Serving HTML
    on the same URL made edge/CDN caches and some WebView fetches return a page
    apps cannot parse → «۰ کانفیگ» while the browser still showed the list.
    Force feed with ?raw=1; force info with ?html=1 (redirect).
    """
    raw = (request.query_params.get("raw") or "").lower()
    if raw in ("1", "true", "yes"):
        return False
    html_q = (request.query_params.get("html") or "").lower()
    if html_q in ("1", "true", "yes"):
        return True

    sec_dest = (request.headers.get("sec-fetch-dest") or "").lower()
    sec_mode = (request.headers.get("sec-fetch-mode") or "").lower()
    # Only address-bar / link navigation → info page. Everything else → feed.
    if sec_dest in ("document", "iframe") or sec_mode == "navigate":
        return True
    return False


def _encode_feed(links: list[str]) -> bytes:
    """Base64 subscription body as bytes (enables explicit Content-Length)."""
    body = "\n".join(x.strip() for x in links if (x or "").strip())
    if not body:
        return b""
    return base64.b64encode(body.encode("utf-8"))


def _share_count_from_body(body: bytes, *, meta: dict | None = None) -> int:
    """Count real share lines. Base64 bodies have ~0 newlines — never use count(\\n)."""
    if meta:
        try:
            n = int(meta.get("share_count") or 0)
            if n > 0:
                return n
        except (TypeError, ValueError):
            pass
    if not body:
        return 0
    try:
        text = base64.b64decode(body, validate=False).decode("utf-8", "ignore")
        n = sum(1 for line in text.splitlines() if (line or "").strip())
        if n > 0:
            return n
    except Exception:  # noqa: BLE001
        pass
    return 1 if body else 0


def _feed_meta_for_user(
    user,
    *,
    complete: bool,
    share_count: int,
) -> dict:
    return {
        "user_id": int(user.id),
        "username": str(user.username or "user"),
        "display_name": getattr(user, "display_name", None),
        "used_traffic": int(user.used_traffic or 0),
        "data_limit": int(user.data_limit or 0),
        "expire": user.expire,
        "status": getattr(user.status, "value", user.status),
        "complete": bool(complete),
        "share_count": int(share_count),
    }


async def _bg_ranked_feed_rebuild(
    user_id: int,
    token: str,
    rank_operator: str | None,
) -> None:
    """Upgrade emergency/fast body to ranked collect without blocking Update."""
    uid = int(user_id)
    now = time.monotonic()
    last = float(_bg_rank_last.get(uid, 0.0) or 0.0)
    if now - last < _BG_RANK_INTERVAL:
        return
    _bg_rank_last[uid] = now
    if len(_bg_rank_last) > 8000:
        cutoff = now - 600.0
        for k, ts in list(_bg_rank_last.items()):
            if ts < cutoff:
                _bg_rank_last.pop(k, None)
    await asyncio.sleep(0.05)
    try:
        async with _bg_rank_sem:
            async with AsyncSessionLocal() as db:
                user = await load_user_by_token(db, token)
                if user is None or int(user.id) != uid:
                    return
                if user.status in (
                    UserStatus.limited,
                    UserStatus.expired,
                    UserStatus.disabled,
                ):
                    return
                try:
                    links = await asyncio.wait_for(
                        _collect_feed_links(
                            user,
                            include_hidden=False,
                            db=db,
                            rank_operator=rank_operator,
                        ),
                        timeout=8.0,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "bg ranked collect user=%s: %s", user.username, exc
                    )
                    return
                cleaned: list[str] = []
                seen_l: set[str] = set()
                for x in links or []:
                    s = _sanitize_vpn_share_link(x or "")
                    if not s or s in seen_l:
                        continue
                    seen_l.add(s)
                    cleaned.append(s)
                links = cleaned
                from app.adapters.http_utils import is_share_link as _isl

                real_n = sum(
                    1
                    for x in links
                    if _isl(x)
                    and "127.0.0.1" not in (x or "")
                    and "localhost" not in (x or "").lower()
                )
                if real_n <= 0:
                    return
                body = _encode_feed(links)
                if not body:
                    return
                _feed_cache_put(
                    uid,
                    False,
                    body,
                    complete=True,
                    rank_operator=rank_operator,
                )
                _token_feed_put(
                    token,
                    body,
                    _feed_meta_for_user(user, complete=True, share_count=real_n),
                    rank_operator=rank_operator,
                )
                logger.info(
                    "bg ranked-feed user=%s n=%s op=%s",
                    user.username,
                    real_n,
                    rank_operator or "global",
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("bg ranked rebuild user_id=%s: %s", uid, exc)


def _schedule_bg_ranked_rebuild(
    user_id: int, token: str, rank_operator: str | None
) -> None:
    try:
        asyncio.create_task(
            _bg_ranked_feed_rebuild(int(user_id), token, rank_operator),
            name=f"astra-sub-rank-{int(user_id)}",
        )
    except RuntimeError:
        pass


# V2Box / Happ / v2rayNG often reject the *entire* sub import when one URI is
# absurdly long or uses non-standard encryption= blobs (mlkem… / PQ experiments).
_MAX_VPN_SHARE_URI = 1800


def _sanitize_vpn_share_link(text: str) -> str | None:
    """Return a client-safe share URI, or None to drop from VPN feeds."""
    raw = (text or "").strip()
    if not raw:
        return None
    if len(raw) > _MAX_VPN_SHARE_URI:
        return None
    if "@[" in raw or _looks_like_ipv6_host(raw):
        return None
    low = raw.lower()
    if "127.0.0.1" in low or "localhost" in low or "@[::1]" in low:
        return None
    # Strip huge ``extra=`` JSON that blows URI size / breaks OkHttp parsers.
    if "extra=" in low:
        try:
            from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

            frag = ""
            main = raw
            if "#" in raw:
                main, frag = raw.split("#", 1)
            parts = urlsplit(main)
            q = [
                (k, v)
                for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if k.lower() != "extra"
            ]
            main = urlunsplit(
                (parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment)
            )
            raw = f"{main}#{frag}" if frag else main
        except Exception:  # noqa: BLE001
            return None
        if len(raw) > _MAX_VPN_SHARE_URI:
            return None
    # Non-standard vless encryption (must be ``none`` for almost all clients).
    if raw.lower().startswith("vless://") and "encryption=" in low:
        try:
            from urllib.parse import parse_qs, urlsplit

            q = parse_qs(urlsplit(raw.split("#", 1)[0]).query)
            enc = (q.get("encryption") or [""])[0].strip().lower()
            if enc and enc not in ("none", "0", "zero"):
                return None
        except Exception:  # noqa: BLE001
            return None
    return raw


def _filter_emergency_link_texts(
    raw_links: list, *, cap: int, seen: set[str] | None = None
) -> list[str]:
    """Filter raw DB link strings into a capped emergency share list."""
    from app.adapters.http_utils import is_share_link
    from app.services.orchestrator import _config_hidden

    out: list[str] = []
    seen = seen if seen is not None else set()
    for link in raw_links or []:
        text = (link or "").strip() if isinstance(link, str) else str(link or "").strip()
        if not text or text in seen or not is_share_link(text):
            continue
        if _config_hidden(text):
            continue
        text = _sanitize_vpn_share_link(text)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= cap:
            break
    return out


def _stamp_group_tags_on_links(
    user,
    rows: list[dict],
) -> list[str]:
    """Rewrite share-link remarks with group ``config_tag`` (نامحدود حجم…).

    Emergency/fast paths used to return raw panel remarks (Turbo / flags) and
    skip the text admins typed on Groups — stamp tags here so Update always
    shows ``#1. نامحدود حجمDبدون ضریب … | user``.
    """
    from app.services.link_remark import apply_display_name

    if not rows:
        return []
    used = int(getattr(user, "used_traffic", 0) or 0)
    limit = int(getattr(user, "data_limit", 0) or 0)
    uname = (getattr(user, "username", None) or "user").strip() or "user"
    st = getattr(user, "status", None)
    user_status = st.value if hasattr(st, "value") else str(st or "")
    renamed = apply_display_name(
        rows,
        getattr(user, "display_name", None),
        used=used,
        limit=limit,
        username=uname,
        user_status=user_status,
    )
    # Status/notice rows use 127.0.0.1 — VPN sanitize drops them; ranked
    # collect re-adds a proper status line. Here we only stamp real configs.
    out: list[str] = []
    seen: set[str] = set()
    for item in renamed:
        if item.get("kind") in ("status", "promo", "notice"):
            continue
        link = _sanitize_vpn_share_link(str(item.get("link") or ""))
        if not link or link in seen:
            continue
        seen.add(link)
        out.append(link)
    return out


def _emergency_links_from_accounts(user, *, max_n: int | None = None) -> list[str]:
    """Fast path — cached share URIs **with group config_tag names**."""
    from app.services.ping_category_order import SUB_MAX_CONFIGS

    cap = int(max_n if max_n is not None else SUB_MAX_CONFIGS)
    if cap <= 0:
        cap = int(SUB_MAX_CONFIGS)

    # panel_id → first non-empty group tag (fallback when account.group_id empty)
    panel_tags: dict[int, str] = {}
    for g in list(getattr(user, "groups", None) or []):
        tag = str(getattr(g, "config_tag", None) or "").strip()
        if not tag:
            continue
        for gp in list(getattr(g, "group_panels", None) or getattr(g, "panels", None) or []):
            try:
                pid = int(getattr(gp, "panel_id", None) or getattr(gp, "id", 0) or 0)
            except (TypeError, ValueError):
                pid = 0
            if pid and pid not in panel_tags:
                panel_tags[pid] = tag

    tagged: list[dict] = []
    seen: set[str] = set()
    for account in user.panel_accounts or []:
        st = getattr(account, "status", None)
        if st not in (
            PanelAccountStatus.active,
            PanelAccountStatus.error,
            PanelAccountStatus.pending,
        ):
            continue
        panel = account.__dict__.get("panel") or getattr(account, "panel", None)
        if panel is not None and not bool(getattr(panel, "is_active", True)):
            continue
        try:
            pid = int(getattr(account, "panel_id", 0) or 0)
        except (TypeError, ValueError):
            pid = 0
        gid = getattr(account, "group_id", None)
        tag = ""
        if gid is not None:
            for g in list(getattr(user, "groups", None) or []):
                if int(getattr(g, "id", 0) or 0) == int(gid):
                    tag = str(getattr(g, "config_tag", None) or "").strip()
                    break
        if not tag and pid:
            tag = panel_tags.get(pid, "")
        chunk = _filter_emergency_link_texts(
            list(account.links or []), cap=max(0, cap - len(tagged)), seen=seen
        )
        for link in chunk:
            tagged.append(
                {
                    "link": link,
                    "panel_id": pid,
                    "group_id": int(gid) if gid is not None else None,
                    "config_tag": tag,
                }
            )
            if len(tagged) >= cap:
                return _stamp_group_tags_on_links(user, tagged)
    return _stamp_group_tags_on_links(user, tagged)


async def _sql_emergency_share_links(
    db: AsyncSession, user, *, max_n: int | None = None
) -> list[str]:
    """SQL share URIs with group ``config_tag`` stamped into remarks."""
    from sqlalchemy import text

    from app.services.ping_category_order import SUB_MAX_CONFIGS

    user_id = int(getattr(user, "id", 0) or 0)
    cap = int(max_n if max_n is not None else SUB_MAX_CONFIGS)
    if cap <= 0:
        cap = int(SUB_MAX_CONFIGS)
    fetch_n = max(cap * 4, 200)
    try:
        result = await db.execute(
            text(
                """
                SELECT t.link,
                       pa.panel_id,
                       pa.group_id,
                       coalesce(
                         nullif(trim(g.config_tag), ''),
                         nullif(trim(g2.config_tag), ''),
                         ''
                       ) AS config_tag
                FROM panel_accounts pa
                LEFT JOIN panels p ON p.id = pa.panel_id
                LEFT JOIN groups g ON g.id = pa.group_id
                LEFT JOIN LATERAL (
                    SELECT gg.config_tag
                    FROM user_groups ug
                    JOIN group_panels gp ON gp.group_id = ug.group_id
                      AND gp.panel_id = pa.panel_id
                    JOIN groups gg ON gg.id = ug.group_id
                    WHERE ug.user_id = pa.user_id
                      AND coalesce(trim(gg.config_tag), '') <> ''
                    ORDER BY ug.group_id
                    LIMIT 1
                ) g2 ON true
                CROSS JOIN LATERAL jsonb_array_elements_text(
                    CASE
                      WHEN pa.links IS NULL THEN '[]'::jsonb
                      WHEN jsonb_typeof(pa.links::jsonb) = 'array'
                        THEN pa.links::jsonb
                      ELSE '[]'::jsonb
                    END
                ) AS t(link)
                WHERE pa.user_id = :uid
                  AND pa.status IN ('active', 'error', 'pending')
                  AND COALESCE(p.is_active, true) = true
                  AND t.link IS NOT NULL
                  AND length(t.link) > 12
                ORDER BY pa.id
                LIMIT :lim
                """
            ),
            {"uid": user_id, "lim": int(fetch_n)},
        )
        raw_rows = result.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("sql emergency links user_id=%s: %s", user_id, exc)
        return []

    tagged: list[dict] = []
    seen: set[str] = set()
    for link, panel_id, group_id, config_tag in raw_rows:
        text_l = _sanitize_vpn_share_link(str(link or "").strip())
        if not text_l or text_l in seen:
            continue
        from app.adapters.http_utils import is_share_link
        from app.services.orchestrator import _config_hidden

        if not is_share_link(text_l) or _config_hidden(text_l):
            continue
        seen.add(text_l)
        tagged.append(
            {
                "link": text_l,
                "panel_id": int(panel_id or 0),
                "group_id": int(group_id) if group_id is not None else None,
                "config_tag": str(config_tag or "").strip(),
            }
        )
        if len(tagged) >= cap:
            break
    return _stamp_group_tags_on_links(user, tagged)


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


def _gone_sub_html(*, brand: str = "") -> str:
    """Polished page when the subscription token is missing / revoked."""
    title = (brand or "اشتراک").replace("<", "").replace(">", "")[:48] or "اشتراک"
    return f"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<meta name="robots" content="noindex,nofollow"/>
<title>لینک نامعتبر · {title}</title>
<style>
  :root {{
    --bg0:#061018; --bg1:#0b1c28; --card:#0f2433;
    --line:rgba(140,200,190,.14); --text:#e8f7f2; --muted:#7fa39a;
    --accent:#3dbea0; --warn:#e8b86d;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; min-height:100vh; color:var(--text);
    font-family:"Vazirmatn",Tahoma,system-ui,sans-serif;
    background:
      radial-gradient(900px 420px at 85% -10%, rgba(61,190,160,.16), transparent 55%),
      radial-gradient(700px 380px at -10% 110%, rgba(232,184,109,.10), transparent 50%),
      linear-gradient(165deg, var(--bg0), var(--bg1) 55%, #08151f);
    display:grid; place-items:center; padding:28px 18px;
  }}
  .card {{
    width:min(440px,100%);
    background:linear-gradient(180deg, rgba(15,36,51,.92), rgba(8,22,32,.96));
    border:1px solid var(--line); border-radius:22px;
    padding:28px 24px 22px; text-align:center;
    box-shadow:0 24px 60px rgba(0,0,0,.35);
    animation:rise .45s ease both;
  }}
  @keyframes rise {{
    from {{ opacity:0; transform:translateY(12px); }}
    to {{ opacity:1; transform:none; }}
  }}
  .mark {{
    width:64px; height:64px; margin:0 auto 16px; border-radius:20px;
    display:grid; place-items:center;
    background:rgba(232,184,109,.12); border:1px solid rgba(232,184,109,.28);
    color:var(--warn);
  }}
  .mark svg {{ width:30px; height:30px; }}
  .brand {{
    font-size:.78rem; letter-spacing:.04em; color:var(--accent);
    font-weight:700; margin:0 0 8px; opacity:.9;
  }}
  h1 {{ margin:0 0 10px; font-size:1.28rem; font-weight:800; line-height:1.35; }}
  p {{ margin:0; color:var(--muted); font-size:.95rem; line-height:1.7; }}
  .tips {{
    margin:18px 0 0; padding:14px 14px 12px; text-align:right;
    border-radius:14px; background:rgba(255,255,255,.03);
    border:1px solid var(--line);
  }}
  .tips li {{
    margin:0 0 8px; color:var(--muted); font-size:.86rem; line-height:1.55;
  }}
  .tips li:last-child {{ margin-bottom:0; }}
  .tips strong {{ color:var(--text); font-weight:700; }}
  .foot {{
    margin-top:18px; font-size:.75rem; color:rgba(127,163,154,.75);
  }}
</style>
</head>
<body>
  <main class="card" role="main">
    <div class="mark" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
           stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="9"/>
        <path d="M12 8v5"/>
        <path d="M12 16h.01"/>
      </svg>
    </div>
    <p class="brand">{title}</p>
    <h1>این لینک اشتراک در دسترس نیست</h1>
    <p>لینک حذف شده، منقضی شده، یا اشتباه وارد شده است. اگر اشتراک فعال دارید، لینک جدید را از پشتیبانی بگیرید.</p>
    <ul class="tips">
      <li><strong>حذف‌شده؟</strong> اشتراک توسط پنل غیرفعال یا پاک شده است.</li>
      <li><strong>اشتباه؟</strong> آدرس را کامل از پیام پشتیبانی کپی کنید.</li>
      <li><strong>اشتراک جدید؟</strong> با پشتیبان تماس بگیرید تا لینک تازه برایتان ارسال شود.</li>
    </ul>
    <p class="foot">Subscription link unavailable</p>
  </main>
</body>
</html>"""


def _sub_not_found_response(
    request: Request | None = None, *, force_html: bool = False
) -> Response:
    """Browser → polished HTML; VPN/clients → short clear 404 text."""
    brand = ""
    try:
        brand = str(get_brand_name() or "").strip()
    except Exception:  # noqa: BLE001
        brand = ""
    wants_html = bool(force_html)
    if request is not None and not wants_html:
        accept = (request.headers.get("accept") or "").lower()
        ua = (request.headers.get("user-agent") or "").lower()
        path = str(getattr(request.url, "path", "") or "")
        if "/info" in path:
            wants_html = True
        elif "text/html" in accept:
            wants_html = True
        elif "mozilla" in ua and not any(
            m in ua
            for m in (
                "clash",
                "v2ray",
                "streisand",
                "shadowrocket",
                "quantumult",
                "hiddify",
                "sing-box",
                "okhttp",
            )
        ):
            wants_html = True
    if wants_html:
        return HTMLResponse(_gone_sub_html(brand=brand), status_code=404)
    return PlainTextResponse(
        "این لینک اشتراک حذف شده یا اشتباه است.\n"
        "This subscription link was removed or is invalid.\n",
        status_code=404,
        media_type="text/plain; charset=utf-8",
    )


async def _configs_for_info_fallback(
    db: AsyncSession, user, *, rank_operator: str | None = None
) -> list[dict]:
    """Last-resort rows if detailed collect times out — keep fragment remarks."""
    from urllib.parse import unquote

    links: list[str] = []
    try:
        tok = (getattr(user, "sub_token", None) or "").strip()
        if tok:
            hit = _token_feed_get(tok, rank_operator) or _token_feed_get(tok, None)
            if hit:
                body, _meta = hit
                text = base64.b64decode(body, validate=False).decode(
                    "utf-8", "ignore"
                )
                links = [ln.strip() for ln in text.splitlines() if ln.strip()]
    except Exception:  # noqa: BLE001
        links = []
    if not links:
        try:
            links = await _sql_emergency_share_links(db, user)
        except Exception:  # noqa: BLE001
            links = []
    if not links:
        try:
            links = _emergency_links_from_accounts(user)
        except Exception:  # noqa: BLE001
            links = []
    out: list[dict] = []
    for link in links:
        safe = (link or "").strip()
        if not safe:
            continue
        remark = ""
        if "#" in safe:
            remark = unquote(safe.rsplit("#", 1)[-1]).strip()
        out.append(
            {
                "link": safe,
                "kind": "config",
                "name": remark,
                "remark": remark,
                "tag": "",
            }
        )
    return out


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

    # Primary: full named rows (status / promo / group tags / display names).
    # live_rebuild=False keeps this under the HTML deadline for most users.
    configs: list[dict] = []
    try:
        configs = await asyncio.wait_for(
            collect_links_detailed(
                user,
                include_notices=True,
                db=db,
                live_rebuild=False,
                rank_operator=rank_operator,
            ),
            timeout=5.5,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("sub detailed user=%s: %s", user.username, exc)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        configs = []
    if not configs:
        try:
            configs = await _configs_for_info_fallback(
                db, user, rank_operator=rank_operator
            )
        except Exception:  # noqa: BLE001
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
        "x-astra-html-rev": "342-no-inline-links",
    }
    try:
        from app.services.client_ua import ACCEPT_CH_HEADERS

        # Client Hints only on HTML — never on the Base64 VPN feed.
        html_headers.update(ACCEPT_CH_HEADERS)
    except Exception:  # noqa: BLE001
        pass
    # Explicit length avoids truncated HTML through CF tunnel / edge nginx.
    body = html.encode("utf-8")
    html_headers["content-length"] = str(len(body))
    return Response(
        content=body,
        status_code=200,
        media_type="text/html; charset=utf-8",
        headers=html_headers,
    )


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
        # Named collect needs a bit more than the old 3.5s wall — still under
        # Iran nginx budgets; fallback page only if truly stuck.
        return await asyncio.wait_for(
            _render_info_page_body(
                db, token, user, rank_operator=rank_operator
            ),
            timeout=8.0,
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
async def get_subscription(token: str, request: Request):
    """
    ``/sub/{token}`` is ALWAYS the Base64 VPN feed (Content-Length, no gzip).

    Browser address-bar opens redirect to ``/sub/{token}/info`` so the same
    URL is never HTML for apps (edge cache / Sec-Fetch mis-detect → 0 nodes).

    Feed path is cache-first when membership accounts have ready share links.
    Incomplete accounts get a short hydrate, then we serve; BG maintain fills gaps.

    Must never return 500 through the Iran reverse tunnel — clients show
    "Internal Server Error" and abandon the update.

    Critical: do NOT Depends(get_db) here — token/disk cache hits must serve
    with zero QueuePool checkout (that alone was 2–3s under load).
    """
    is_probe = _wants_probe(request)
    is_html = _wants_html_page(request)
    if is_html and not is_probe:
        # Keep query string (rev / operator hints) off the redirect target.
        return RedirectResponse(
            url=f"/sub/{token}/info",
            status_code=302,
            headers={
                "cache-control": "no-store, no-cache, must-revalidate, max-age=0",
                "cdn-cache-control": "no-store",
            },
        )
    curated_early = (not is_probe) and _wants_curated_feed(request)
    rank_operator_early = (
        _rank_operator_from_request(request) if curated_early else None
    )

    # Zero-DB Update path: token body cache (survives QueuePool / CPU storms).
    # Fallback to global bucket — emergency/fast bodies are ISP-agnostic until
    # bg ranked rewrite; avoids a cold miss per Shatel/Irancell/… on first open.
    if curated_early:
        tok_hit = _token_feed_get(token, rank_operator_early)
        if not tok_hit and rank_operator_early:
            tok_hit = _token_feed_get(token, None)
        if tok_hit:
            body_hit, meta = tok_hit
            st = str(meta.get("status") or "")
            exp_raw = meta.get("expire")
            try:
                exp_i = int(exp_raw or 0)
            except (TypeError, ValueError):
                exp_i = 0
            # Wall-clock expire must invalidate even if cached status is still active
            timed_out = exp_i > 0 and int(time.time()) >= exp_i
            if timed_out or st in (
                UserStatus.limited.value,
                UserStatus.expired.value,
                UserStatus.disabled.value,
                "limited",
                "expired",
                "disabled",
            ):
                try:
                    uid_meta = int(meta.get("user_id") or 0)
                    if uid_meta:
                        invalidate_user_feed_cache(uid_meta)
                except Exception:  # noqa: BLE001
                    pass
            else:
                from types import SimpleNamespace

                proxy = SimpleNamespace(
                    id=int(meta.get("user_id") or 0),
                    username=str(meta.get("username") or "user"),
                    display_name=meta.get("display_name"),
                    used_traffic=int(meta.get("used_traffic") or 0),
                    data_limit=int(meta.get("data_limit") or 0),
                    expire=meta.get("expire"),
                    sub_token=token,
                    status=st,
                )
                try:
                    from app.services.orchestrator import mark_live_usage_priority

                    if proxy.id:
                        mark_live_usage_priority(int(proxy.id))
                        _schedule_bg_usage(int(proxy.id))
                except Exception:  # noqa: BLE001
                    pass

                def _headers_tok(body: bytes) -> dict[str, str]:
                    digest = hashlib.sha256(body or b"").hexdigest()[:12]
                    hdrs = _sub_headers(
                        proxy, body_rev=f"{digest}-{int(time.time())}"
                    )
                    hdrs["x-astra-rank-op"] = (rank_operator_early or "global")[:32]
                    hdrs["x-astra-cache"] = "token"
                    return hdrs

                return _feed_response(
                    body_hit,
                    _headers_tok(body_hit),
                    share_count=_share_count_from_body(body_hit, meta=meta),
                )

    async with AsyncSessionLocal() as db:
        return await _serve_subscription_db(
            token,
            request,
            db,
            is_probe=is_probe,
            is_html=is_html,
            curated_early=curated_early,
            rank_operator_early=rank_operator_early,
        )


async def _serve_subscription_db(
    token: str,
    request: Request,
    db: AsyncSession,
    *,
    is_probe: bool,
    is_html: bool,
    curated_early: bool,
    rank_operator_early: str | None,
):
    # Light load first — VPN Update cache-hit must not join all accounts/groups
    # (that query alone was 3–10s under load → apps «آپدیت نشد»).
    try:
        user = await load_user_by_token_light(db, token)
    except BaseException as exc:  # noqa: BLE001
        if _is_cancel(exc):
            raise
        logger.exception("sub load token=%s…", (token or "")[:8])
        await _safe_rollback(db)
        return PlainTextResponse("Temporarily unavailable", status_code=503)

    if not user:
        return _sub_not_found_response(request, force_html=bool(is_html))

    # Flip expire → expired on the request path (do not wait for quota-watch).
    # Then schedule a hard remote freeze so main panels cut traffic immediately.
    try:
        flipped = await enforce_user_quota(db, user)
        if flipped:
            await db.commit()
            invalidate_user_feed_cache(int(user.id))
            _schedule_bg_maintain(int(user.id), force=True)
    except Exception:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass

    # Snapshot scalars before any rollback/expire — exception paths must not
    # lazy-load ORM attrs (MissingGreenlet → HTTP 500 → VPN «آپدیت نشد»).
    uname_safe = str(getattr(user, "username", None) or "?")
    uid_safe = int(user.id)

    _t0 = time.monotonic()
    _dbg(
        "H7",
        "subscription.py:get_subscription",
        "enter",
        {
            "user_id": uid_safe,
            "username": uname_safe,
            "status": str(user.status),
            "token8": (token or "")[:8],
            "ua": (request.headers.get("user-agent") or "")[:60],
        },
    )

    # Instant Update: light user + feed cache — no heavy ORM, no ensure_loaded.
    # Exhausted users must NEVER hit the active feed cache (configs stay live).
    blocked_early = user.status in (UserStatus.limited, UserStatus.expired)
    if blocked_early:
        try:
            invalidate_user_feed_cache(uid_safe)
        except Exception:  # noqa: BLE001
            pass
        try:
            from app.services.orchestrator import kick_freeze_user

            # force=True bypasses cooldown so a failed partial freeze is retried
            # on the next /sub open instead of waiting 45–180s while traffic bleeds.
            await kick_freeze_user(uid_safe, remote_timeout=10.0, force=True)
        except Exception:  # noqa: BLE001
            pass
    if (
        curated_early
        and user.status != UserStatus.disabled
        and not blocked_early
    ):
        entry_early = _feed_cache_entry(uid_safe, False, rank_operator_early)
        if not entry_early and rank_operator_early:
            entry_early = _feed_cache_entry(uid_safe, False, None)
        if entry_early:
            cached_early, cache_complete = entry_early
            try:
                from app.services.orchestrator import mark_live_usage_priority

                mark_live_usage_priority(uid_safe)
            except Exception:  # noqa: BLE001
                pass
            _schedule_bg_usage(uid_safe)
            # Maintain only on debounce — not every Update tap.
            _schedule_bg_maintain(uid_safe)
            # Emergency/raw cache → keep upgrading to group config_tag names
            if not cache_complete:
                _schedule_bg_ranked_rebuild(
                    uid_safe, token, rank_operator_early
                )
            _sc_early = _share_count_from_body(cached_early)
            _token_feed_put(
                token,
                cached_early,
                _feed_meta_for_user(
                    user,
                    complete=cache_complete,
                    share_count=_sc_early,
                ),
                rank_operator=rank_operator_early,
            )

            def _headers_hit(body: bytes) -> dict[str, str]:
                import hashlib

                digest = hashlib.sha256(body or b"").hexdigest()[:12]
                hdrs = _sub_headers(
                    user, body_rev=f"{digest}-{int(time.time())}"
                )
                hdrs["x-astra-rank-op"] = (rank_operator_early or "global")[:32]
                hdrs["x-astra-cache"] = (
                    "hit" if cache_complete else "hit-partial"
                )
                return hdrs

            return _feed_response(
                cached_early,
                _headers_hit(cached_early),
                share_count=_sc_early,
            )

    # VPN Update cold miss: SQL share links only (no ORM of 80+ accounts).
    # ORM accounts load was still 1–4s for fat users → apps «آپدیت نشد».
    if (
        curated_early
        and not is_probe
        and not blocked_early
        and user.status != UserStatus.disabled
    ):
        try:
            emergency = await _sql_emergency_share_links(db, user)
        except BaseException as exc:  # noqa: BLE001
            if _is_cancel(exc):
                raise
            logger.warning("sub sql-emergency user=%s: %s", uname_safe, exc)
            emergency = []
        if emergency:
            real_n = len(emergency)
            body = _encode_feed(emergency)
            if body and real_n > 0:
                rank_operator = rank_operator_early
                # Tags already stamped from groups.config_tag — safe to cache.
                _meta = _feed_meta_for_user(
                    user, complete=True, share_count=real_n
                )
                # Warm global + ISP buckets so next Update from any operator hits.
                for _op in {None, rank_operator}:
                    _feed_cache_put(
                        uid_safe,
                        False,
                        body,
                        complete=True,
                        rank_operator=_op,
                    )
                    _token_feed_put(
                        token, body, _meta, rank_operator=_op
                    )
                _schedule_bg_ranked_rebuild(uid_safe, token, rank_operator)
                _schedule_bg_maintain(uid_safe)
                try:
                    from app.services.orchestrator import mark_live_usage_priority

                    mark_live_usage_priority(uid_safe)
                    _schedule_bg_usage(uid_safe)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    from app.services.user_analytics import record_sub_fetch

                    hdrs = {k: v for k, v in request.headers.items()}
                    client_host = (
                        request.client.host if request.client else None
                    )
                    asyncio.create_task(
                        record_sub_fetch(
                            user_id=uid_safe,
                            headers=hdrs,
                            client_host=client_host,
                            is_probe=False,
                        ),
                        name=f"astra-sub-fetch-{uid_safe}",
                    )
                except Exception:  # noqa: BLE001
                    pass

                def _headers_fast(b: bytes) -> dict[str, str]:
                    digest = hashlib.sha256(b or b"").hexdigest()[:12]
                    hdrs = _sub_headers(
                        user, body_rev=f"{digest}-{int(time.time())}"
                    )
                    hdrs["x-astra-rank-op"] = (rank_operator or "global")[:32]
                    hdrs["x-astra-cache"] = "fast"
                    return hdrs

                logger.info(
                    "sub sql-fast user=%s n=%s",
                    uname_safe,
                    real_n,
                )
                return _feed_response(
                    body, _headers_fast(body), share_count=real_n
                )

    # Cache miss / HTML / probe / empty: full graph load for collect + claims.
    try:
        user_full = await load_user_by_token(db, token)
        if user_full is not None:
            user = user_full
    except BaseException as exc:  # noqa: BLE001
        if _is_cancel(exc):
            raise
        logger.exception("sub full-load token=%s…", (token or "")[:8])
        await _safe_rollback(db)
        return PlainTextResponse("Temporarily unavailable", status_code=503)

    # Analytics: log sub opens (HTML + feed) so quota-watch prioritizes them
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

    # Live usage MUST NOT block the VPN feed. Waiting on remotes here made
    # Cloudflare/edge return 503 and apps showed «آپدیت نشد» while browser HTML
    # still worked. Hot/bg loops keep «مصرف» fresh; feed returns instantly.
    if not is_probe:
        try:
            from app.services.orchestrator import mark_live_usage_priority

            mark_live_usage_priority(uid_safe)
            _schedule_bg_usage(uid_safe)
        except Exception:  # noqa: BLE001
            pass
    try:
        # HTML is served only from /sub/{token}/info (browser redirects above).
        # This handler is Base64 feed only — never return a page apps cannot parse.

        # Curated 99-cap for normal VPN Update. AstraPing / probe get full inventory.
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
            hdrs["x-astra-cache"] = "miss"
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

        # Completeness: VPN curated skips expensive claims scan on the request
        # path (that query competed with Update under load). BG maintain heals.
        has_links = _user_has_share_links(user)
        if curated:
            complete = has_links
            _schedule_bg_maintain(int(user.id), force=not has_links)
            # First open with no share links: brief hydrate so app isn't blank,
            # still under CF/edge budgets; BG fills the rest.
            if not has_links:
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
                        timeout=1.4,
                    )
                    if n:
                        invalidate_user_feed_cache(int(user.id))
                        user = await load_user_by_token(db, token) or user
                        has_links = _user_has_share_links(user)
                        complete = has_links
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "sub curated-hydrate user=%s: %s",
                        uname_safe,
                        exc,
                    )
                    try:
                        user = await load_user_by_token(db, token) or user
                    except Exception:  # noqa: BLE001
                        pass
        else:
            try:
                complete = await _user_sub_links_complete(db, user)
            except Exception:  # noqa: BLE001
                complete = True
            if not complete:
                _schedule_bg_maintain(int(user.id), force=True)
            # Probe/full may do a short hydrate when completely empty.
            if not has_links:
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
                        timeout=3.0,
                    )
                    if n:
                        invalidate_user_feed_cache(int(user.id))
                        user = await load_user_by_token(db, token) or user
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
            # Kick remote disable NOW — status remark only, no live share links.
            try:
                from app.services.orchestrator import kick_freeze_user

                await kick_freeze_user(int(user.id), remote_timeout=10.0)
            except Exception:  # noqa: BLE001
                pass
            if curated:
                from app.services.link_remark import (
                    build_notice_link,
                    build_status_row,
                )
                from app.services.site_settings import (
                    get_status_line_template,
                    get_status_panel_label,
                )

                st_val = getattr(user.status, "value", user.status)
                st_row = build_status_row(
                    username=(user.username or "user"),
                    used=int(user.used_traffic or 0),
                    limit=int(user.data_limit or 0),
                    expire=user.expire,
                    template=get_status_line_template(),
                    panel_label=get_status_panel_label(),
                    user_status=str(st_val),
                )
                notice = build_notice_link(
                    "⛔️ حجم تمام شد"
                    if st_val == "limited"
                    else "⛔️ اشتراک منقضی شد"
                )
                body = _encode_feed(
                    [st_row["link"]] + ([notice] if notice else [])
                )
                return _feed_response(body, _headers_for(body), share_count=0)
        # Short curated cache keeps Update bursts under CF limits (no 503).
        # Probe/full always rebuilds.
        if curated and not blocked:
            cached = _feed_cache_get(int(user.id), False, rank_operator)
            if cached:
                return _feed_response(
                    cached,
                    _headers_for(cached),
                    share_count=_share_count_from_body(cached),
                )

        from app.adapters.http_utils import is_share_link as _isl

        def _count_real(items: list[str]) -> int:
            return sum(
                1
                for x in items
                if _isl(x)
                and "127.0.0.1" not in (x or "")
                and "localhost" not in (x or "").lower()
            )

        # VPN Update (V2Box / Happ / v2rayNG): serve DB share links NOW.
        # Ranked collect was 0.7–8s under load → apps showed empty / no update.
        # Emergency body caches; bg upgrades to ISP-ranked order within seconds.
        if curated and not blocked and has_links:
            emergency = _emergency_links_from_accounts(user)
            if emergency:
                real_n = _count_real(emergency)
                body = _encode_feed(emergency)
                if body and real_n > 0:
                    # Group config_tag already stamped on remarks
                    _feed_cache_put(
                        int(user.id),
                        False,
                        body,
                        complete=True,
                        rank_operator=rank_operator,
                    )
                    _token_feed_put(
                        token,
                        body,
                        _feed_meta_for_user(
                            user, complete=True, share_count=real_n
                        ),
                        rank_operator=rank_operator,
                    )
                    _schedule_bg_ranked_rebuild(
                        int(user.id), token, rank_operator
                    )
                    _schedule_bg_maintain(int(user.id))
                    logger.info(
                        "sub fast-emergency user=%s n=%s (ranked bg)",
                        uname_safe,
                        real_n,
                    )
                    _fast_hdrs = _headers_for(body)
                    _fast_hdrs["x-astra-cache"] = "fast"
                    return _feed_response(
                        body, _fast_hdrs, share_count=real_n
                    )

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
                # Empty / no emergency — short collect; never sit on CF 502.
                links = await asyncio.wait_for(
                    _collect_feed_links(
                        user,
                        include_hidden=include_hidden,
                        db=db,
                        rank_operator=rank_operator,
                    ),
                    timeout=25.0 if include_hidden else 1.8,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("sub collect user=%s: %s", user.username, exc)
            links = []

        # VPN apps reject the whole import on one bad/huge URI (IPv6 / PQ enc).
        if curated:
            _seen_c: set[str] = set()
            _clean_c: list[str] = []
            for x in links:
                s = _sanitize_vpn_share_link(x or "")
                if not s or s in _seen_c:
                    continue
                _seen_c.add(s)
                _clean_c.append(s)
            links = _clean_c

        real_n = _count_real(links)

        # Empty / sparse feed: keep CF/Iran tunnel budgets tight.
        # Sparse-but-nonempty returns immediately; empty gets hydrate + emergency.
        if not include_hidden and real_n < 55:
            _schedule_bg_maintain(int(user.id), force=True)
            if real_n <= 0 and curated:
                try:
                    from app.services.orchestrator import hydrate_share_links_pool_safe

                    n = await asyncio.wait_for(
                        hydrate_share_links_pool_safe(int(user.id)),
                        timeout=1.6,
                    )
                    if n:
                        user = await load_user_by_token(db, token) or user
                        emergency = _emergency_links_from_accounts(user)
                        if emergency:
                            links = emergency
                        else:
                            links = await asyncio.wait_for(
                                _collect_feed_links(
                                    user,
                                    include_hidden=False,
                                    db=db,
                                    rank_operator=rank_operator,
                                ),
                                timeout=1.5,
                            )
                        _seen_r: set[str] = set()
                        _clean_r: list[str] = []
                        for x in links:
                            s = _sanitize_vpn_share_link(x or "")
                            if not s or s in _seen_r:
                                continue
                            _seen_r.add(s)
                            _clean_r.append(s)
                        links = _clean_r
                        real_n = _count_real(links)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "sub empty-recollect user=%s: %s", uname_safe, exc
                    )
            elif real_n <= 0:
                logger.info(
                    "sub empty-bg-only user=%s curated=%s",
                    uname_safe,
                    int(bool(curated)),
                )
            else:
                logger.info(
                    "sub sparse-bg-only user=%s real_n=%s (no request hydrate)",
                    uname_safe,
                    real_n,
                )

        # Last resort: ranked collect empty but DB already has share links
        # (timeout / claims blip). Serve raw account links so apps never get 0.
        if curated and real_n <= 0 and _user_has_share_links(user):
            emergency = _emergency_links_from_accounts(user)
            if emergency:
                logger.warning(
                    "sub emergency-feed user=%s n=%s (collect empty)",
                    uname_safe,
                    len(emergency),
                )
                links = emergency
                real_n = _count_real(links)

        body = _encode_feed(links)
        if not body or real_n <= 0:
            logger.warning(
                "sub returning empty body user=%s token=%s… real=%s",
                user.username,
                token[:8],
                real_n,
            )
        elif curated and real_n > 0 and not blocked:
            _complete = bool(complete and real_n >= 40)
            _feed_cache_put(
                int(user.id),
                False,
                body,
                complete=_complete,
                rank_operator=rank_operator,
            )
            _token_feed_put(
                token,
                body,
                _feed_meta_for_user(
                    user, complete=_complete, share_count=real_n
                ),
                rank_operator=rank_operator,
            )
            if not include_hidden:
                _schedule_bg_ranked_rebuild(int(user.id), token, rank_operator)

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

    Instant DB recount from panel meters, then schedule a background remote
    pull so the next poll tracks downloads in near real-time. Never blocks on
    remote HTTP — keeps Iran tunnel under the 504 wall.
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
        from app.services.orchestrator import (
            mark_live_usage_priority,
            recount_user_used_traffic,
        )

        mark_live_usage_priority(int(user.id))
        await recount_user_used_traffic(db, user)
        await db.commit()
    except Exception:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass

    try:
        _schedule_bg_usage(int(user.id))
    except Exception:  # noqa: BLE001
        pass

    status = user.status.value if hasattr(user.status, "value") else str(user.status)
    used = int(user.used_traffic or 0)
    limit = int(user.data_limit or 0)
    remain = None if limit <= 0 else max(0, limit - used)
    pct = None
    if limit > 0:
        pct = min(100.0, round(used / limit * 1000) / 10)
    return JSONResponse(
        {
            "username": user.username,
            "status": status,
            "used": used,
            "limit": limit,
            "remain": remain,
            "pct": pct,
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
    # VPN apps often paste the browser URL (…/info). HTML cannot be imported →
    # redirect those UAs to the Base64 feed. Real browsers always get HTML
    # (do NOT require Sec-Fetch — mobile/proxy often omit it and used to 302
    # away from the named config list).
    ua = (request.headers.get("user-agent") or "").lower()
    vpn_markers = (
        "v2ray",
        "v2box",
        "happ",
        "clash",
        "sing-box",
        "singbox",
        "shadowrocket",
        "streisand",
        "hiddify",
        "nekobox",
        "okhttp",
        "surge",
        "quantumult",
        "stash",
        "loon",
    )
    accept = (request.headers.get("accept") or "").lower()
    wants_html = "text/html" in accept or not accept
    is_vpn_ua = any(m in ua for m in vpn_markers)
    if is_vpn_ua and not wants_html:
        return RedirectResponse(
            url=f"/sub/{token}",
            status_code=302,
            headers={"cache-control": "no-store"},
        )
    if is_vpn_ua and "text/html" not in accept and "mozilla" not in ua:
        return RedirectResponse(
            url=f"/sub/{token}",
            status_code=302,
            headers={"cache-control": "no-store"},
        )
    try:
        # Full graph required — group config_tag / display names come from claims.
        user = await load_user_by_token(db, token)
    except BaseException as exc:  # noqa: BLE001
        if _is_cancel(exc):
            raise
        logger.exception("sub info load token=%s…", (token or "")[:8])
        await _safe_rollback(db)
        return HTMLResponse(_fallback_sub_html(), status_code=200)
    if not user:
        return _sub_not_found_response(request, force_html=True)
    return await _render_info_page(
        db,
        token,
        user,
        rank_operator=_rank_operator_from_request(request),
    )
