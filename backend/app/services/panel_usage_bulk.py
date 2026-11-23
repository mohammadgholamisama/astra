"""Bulk remote usage sync — one HTTP list updates thousands of accounts.

Marzban / PasarGuard / Rebecca expose GET /api/users. Pulling every Astra
user one-by-one cannot scale; this keeps quota accurate for large installs.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.adapters import get_adapter
from app.adapters.marzban import MarzbanAdapter
from app.core.database import AsyncSessionLocal
from app.models.panel import Panel, PanelType
from app.models.user import PanelAccount, PanelAccountStatus, User, UserStatus

logger = logging.getLogger("astra.usage")

# foo_g12 / foo_p3 / foo_p3a1 — Astra aliases on shared panels
_ALIAS_RE = re.compile(
    r"^(?P<base>.+?)_(?P<kind>[gp])(?P<num>\d+)(?P<extra>[0-9a-fA-F]*)?$",
    re.IGNORECASE,
)

_BULK_TYPES = {
    PanelType.pasarguard,
    PanelType.marzban,
    PanelType.rebecca,
    PanelType.sanaei,
}
_ROTATE_CURSOR = 0

# Set on panel_accounts after a successful remote quota freeze.
QUOTA_FROZEN_MARK = "ASTRA_QUOTA_FROZEN"


def _as_int(raw: object) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def parse_user_used_traffic(data: dict[str, Any]) -> int:
    """Current-period used bytes (Marzban / PasarGuard / Rebecca).

    Prefer ``used_traffic``. Some PasarGuard list payloads omit/zero the field
    while ``lifetime_used_traffic`` still reflects real consumption — use
    lifetime only when the current key is missing (not when it is legitimately 0
    after a reset).
    """
    has_used_key = "used_traffic" in data
    used = _as_int(data.get("used_traffic"))
    if has_used_key and used is not None and used > 0:
        return used
    for key in ("current_usage", "total_usage"):
        got = _as_int(data.get(key))
        if got is not None and got > 0:
            return got
    traffic = data.get("traffic")
    if isinstance(traffic, dict):
        for key in ("used", "used_traffic", "total"):
            got = _as_int(traffic.get(key))
            if got is not None and got > 0:
                return got
        try:
            up = int(traffic.get("upload") or traffic.get("up") or 0)
            down = int(traffic.get("download") or traffic.get("down") or 0)
            if up or down:
                return max(0, up + down)
        except (TypeError, ValueError):
            pass
    for key in ("lifetime_used_traffic", "total_traffic"):
        got = _as_int(data.get(key))
        if got is not None and got > 0:
            return got
    # Explicit zero from used_traffic key (reset / unused)
    if has_used_key and used is not None:
        return used
    return 0


async def fetch_sanaei_usage_map(panel: Panel) -> dict[str, int]:
    """email → used bytes from one Sanaei ``inbounds/list`` (all clients)."""
    from app.adapters.sanaei import SanaeiAdapter

    adapter = get_adapter(panel)
    if not isinstance(adapter, SanaeiAdapter):
        close = getattr(adapter, "close", None)
        if callable(close):
            try:
                await close()
            except Exception:  # noqa: BLE001
                pass
        return {}
    out: dict[str, int] = {}
    try:
        inbounds = await adapter._raw_inbounds_for_usage()
        for inbound in inbounds or []:
            if not isinstance(inbound, dict):
                continue
            stats = inbound.get("clientStats") or []
            hit = False
            if isinstance(stats, list):
                for stat in stats:
                    if not isinstance(stat, dict):
                        continue
                    email = str(stat.get("email") or "").strip()
                    if not email:
                        continue
                    hit = True
                    got = int(stat.get("up") or 0) + int(stat.get("down") or 0)
                    out[email] = int(out.get(email) or 0) + max(0, got)
            if hit:
                continue
            settings_raw = inbound.get("settings")
            settings: Any = settings_raw
            if isinstance(settings_raw, str):
                try:
                    import json as _json

                    settings = _json.loads(settings_raw)
                except Exception:  # noqa: BLE001
                    settings = {}
            if not isinstance(settings, dict):
                continue
            for client in settings.get("clients") or []:
                if not isinstance(client, dict):
                    continue
                email = str(client.get("email") or "").strip()
                if not email:
                    continue
                got = int(client.get("up") or 0) + int(client.get("down") or 0)
                out[email] = int(out.get(email) or 0) + max(0, got)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sanaei bulk usage failed panel=%s: %s", panel.id, exc)
    finally:
        close = getattr(adapter, "close", None)
        if callable(close):
            try:
                await close()
            except Exception:  # noqa: BLE001
                pass
    return out


async def fetch_panel_usage_map(
    panel: Panel, *, max_pages: int = 80, page_size: int = 200
) -> dict[str, int]:
    """username → used_traffic bytes via GET /api/users (paginated)."""
    if panel.type not in _BULK_TYPES:
        return {}
    if panel.type == PanelType.sanaei:
        return await fetch_sanaei_usage_map(panel)
    adapter = get_adapter(panel)
    if not isinstance(adapter, MarzbanAdapter):
        close = getattr(adapter, "close", None)
        if callable(close):
            try:
                await close()
            except Exception:  # noqa: BLE001
                pass
        return {}

    out: dict[str, int] = {}
    try:
        client, headers = await adapter._authed()
        timeout = httpx.Timeout(25.0, connect=8.0)
        offset = 0
        for _ in range(max(1, int(max_pages))):
            resp = await client.get(
                "/api/users",
                headers=headers,
                params={"limit": int(page_size), "offset": int(offset)},
                timeout=timeout,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "bulk users HTTP %s panel=%s", resp.status_code, panel.id
                )
                break
            try:
                payload = resp.json()
            except Exception:  # noqa: BLE001
                break
            users = []
            if isinstance(payload, dict):
                users = payload.get("users") or payload.get("items") or []
            elif isinstance(payload, list):
                users = payload
            if not isinstance(users, list) or not users:
                break
            for row in users:
                if not isinstance(row, dict):
                    continue
                uname = str(row.get("username") or "").strip()
                used = parse_user_used_traffic(row)
                if uname:
                    # Keep the larger reading if the same name appears twice
                    out[uname] = max(int(out.get(uname) or 0), used)
                # PasarGuard also keys remotes by numeric id in remote_id
                uid = row.get("id")
                if uid is not None and str(uid).strip():
                    kid = str(uid).strip()
                    out[kid] = max(int(out.get(kid) or 0), used)
            if len(users) < int(page_size):
                break
            offset += int(page_size)
            await asyncio.sleep(0.02)
    except Exception as exc:  # noqa: BLE001
        logger.warning("bulk users failed panel=%s: %s", panel.id, exc)
    finally:
        close = getattr(adapter, "close", None)
        if callable(close):
            try:
                await close()
            except Exception:  # noqa: BLE001
                pass
    return out


def _build_usage_indexes(
    usage_map: dict[str, int],
) -> tuple[dict[str, int], dict[str, int], dict[tuple[str, str, int], int]]:
    """exact, lower, and (base, g|p, num) alias indexes."""
    usage_exact = {
        str(k).strip(): int(v) for k, v in usage_map.items() if str(k).strip()
    }
    usage_lower = {k.lower(): v for k, v in usage_exact.items()}
    alias_idx: dict[tuple[str, str, int], int] = {}
    for name, used in usage_exact.items():
        m = _ALIAS_RE.match(name)
        if not m:
            continue
        base = (m.group("base") or "").strip().lower()
        kind = (m.group("kind") or "").lower()
        try:
            num = int(m.group("num") or 0)
        except (TypeError, ValueError):
            continue
        if not base or kind not in ("g", "p") or num <= 0:
            continue
        key = (base, kind, num)
        prev = alias_idx.get(key)
        alias_idx[key] = used if prev is None else max(prev, used)
    return usage_exact, usage_lower, alias_idx


def _lookup_usage_bytes(
    usage_exact: dict[str, int],
    usage_lower: dict[str, int],
    alias_idx: dict[tuple[str, str, int], int],
    *,
    astra_username: str,
    remote_username: str | None,
    remote_id: str | None,
    panel_id: int,
    group_id: int | None,
) -> int | None:
    """Match remote meters across base / ``_g`` / ``_p`` aliases."""
    from app.services.orchestrator import (
        _remote_username_candidates,
        sanitize_remote_username,
    )

    names = _remote_username_candidates(
        astra_username=astra_username or "",
        remote_username=remote_username,
        remote_id=remote_id,
        uuid=None,
        panel_id=int(panel_id),
        group_id=int(group_id) if group_id is not None else None,
    )
    for name in names:
        if name in usage_exact:
            return int(usage_exact[name])
        hit = usage_lower.get(name.lower())
        if hit is not None:
            return int(hit)

    base = sanitize_remote_username(astra_username or "").lower()
    if not base:
        return None
    if group_id is not None:
        hit = alias_idx.get((base, "g", int(group_id)))
        if hit is not None:
            return int(hit)
    hit = alias_idx.get((base, "p", int(panel_id)))
    if hit is not None:
        return int(hit)
    return None


def _apply_got_to_account(
    acc: PanelAccount, got: int, *, now: datetime, dirty: set[int]
) -> None:
    prev = int(acc.used_traffic or 0)
    if got < prev:
        return
    if got == prev:
        acc.last_sync_at = now
        return
    acc.used_traffic = got
    acc.last_sync_at = now
    if (acc.error_message or "") == QUOTA_FROZEN_MARK:
        acc.error_message = None
    if acc.status == PanelAccountStatus.error:
        acc.status = PanelAccountStatus.active
        if (acc.error_message or "") != QUOTA_FROZEN_MARK:
            acc.error_message = None
    dirty.add(int(acc.user_id))


async def apply_usage_map_to_panel(
    db: AsyncSession, panel_id: int, usage_map: dict[str, int]
) -> set[int]:
    """Write raw counters onto panel_accounts. Returns dirty user ids."""
    if not usage_map:
        return set()
    usage_exact, usage_lower, alias_idx = _build_usage_indexes(usage_map)
    result = await db.execute(
        select(PanelAccount, User.username)
        .join(User, User.id == PanelAccount.user_id)
        .where(
            PanelAccount.panel_id == int(panel_id),
            PanelAccount.status.in_(
                (
                    PanelAccountStatus.active,
                    PanelAccountStatus.error,
                )
            ),
        )
    )
    rows = list(result.all())
    now = datetime.now(timezone.utc)
    dirty: set[int] = set()
    matched = 0
    for acc, astra_uname in rows:
        got_i = _lookup_usage_bytes(
            usage_exact,
            usage_lower,
            alias_idx,
            astra_username=str(astra_uname or ""),
            remote_username=(acc.remote_username or "").strip() or None,
            remote_id=str(acc.remote_id).strip()
            if acc.remote_id and not str(acc.remote_id).startswith("{")
            else None,
            panel_id=int(panel_id),
            group_id=int(acc.group_id) if getattr(acc, "group_id", None) is not None else None,
        )
        if got_i is None:
            continue
        matched += 1
        _apply_got_to_account(acc, int(got_i), now=now, dirty=dirty)
    if rows and matched < max(1, len(rows) // 4):
        logger.warning(
            "bulk match sparse panel=%s accounts=%s matched=%s map=%s dirty=%s",
            panel_id,
            len(rows),
            matched,
            len(usage_exact),
            len(dirty),
        )
    return dirty


async def heal_zero_accounts_via_get_usage(
    panel: Panel,
    *,
    limit: int = 400,
    concurrency: int = 20,
) -> set[int]:
    """Per-remote get_usage for accounts still at 0 after bulk map miss.

    Covers PasarGuard/Marzban aliases that the list map did not match
    (``_g`` / ``_p`` / empty remote_username).
    """
    from app.services.orchestrator import _remote_username_candidates

    if panel.type not in (
        PanelType.pasarguard,
        PanelType.marzban,
        PanelType.rebecca,
        PanelType.sanaei,
    ):
        return set()
    pid = int(panel.id)
    # Prefer never-synced / unknown-remote first (most likely alias miss).
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(PanelAccount, User.username)
                .join(User, User.id == PanelAccount.user_id)
                .where(
                    PanelAccount.panel_id == pid,
                    PanelAccount.status.in_(
                        (
                            PanelAccountStatus.active,
                            PanelAccountStatus.error,
                        )
                    ),
                    PanelAccount.used_traffic <= 0,
                )
                .order_by(
                    PanelAccount.last_sync_at.asc().nullsfirst(),
                    PanelAccount.id.desc(),
                )
                .limit(max(1, int(limit)))
            )
        ).all()
        # Touch panel attrs for detached get_adapter use
        _ = (
            panel.id,
            panel.type,
            panel.name,
            panel.is_active,
            getattr(panel, "base_url", None),
            getattr(panel, "url", None),
            getattr(panel, "username", None),
            getattr(panel, "password", None),
            getattr(panel, "token", None),
            getattr(panel, "meta", None),
        )

    if not rows:
        return set()

    jobs: list[tuple[int, int, list[str]]] = []
    for acc, astra_uname in rows:
        names = _remote_username_candidates(
            astra_username=str(astra_uname or ""),
            remote_username=(acc.remote_username or "").strip() or None,
            remote_id=str(acc.remote_id).strip()
            if acc.remote_id and not str(acc.remote_id).startswith("{")
            else None,
            uuid=None,
            panel_id=pid,
            group_id=int(acc.group_id)
            if getattr(acc, "group_id", None) is not None
            else None,
        )
        if not names:
            continue
        jobs.append((int(acc.id), int(acc.user_id), names))

    if not jobs:
        return set()

    sem = asyncio.Semaphore(max(4, int(concurrency)))
    found: dict[int, tuple[int, str]] = {}  # account_id -> (used, remote_name)

    async def _one(aid: int, names: list[str]) -> None:
        async with sem:
            adapter = get_adapter(panel)
            try:
                for name in names:
                    if not name:
                        continue
                    try:
                        try:
                            res = await asyncio.wait_for(
                                adapter.get_usage(  # type: ignore[call-arg]
                                    name, name, expand_links=False
                                ),
                                timeout=8.0,
                            )
                        except TypeError:
                            res = await asyncio.wait_for(
                                adapter.get_usage(name, name),
                                timeout=8.0,
                            )
                    except Exception:  # noqa: BLE001
                        continue
                    if res is not None and res.success:
                        got = int(res.used_traffic or 0)
                        if got > 0:
                            hit_name = (
                                str(res.remote_username or "").strip() or name
                            )
                            found[aid] = (got, hit_name)
                            return
            finally:
                close = getattr(adapter, "close", None)
                if callable(close):
                    try:
                        await close()
                    except Exception:  # noqa: BLE001
                        pass

    await asyncio.gather(
        *[_one(aid, names) for aid, _uid, names in jobs],
        return_exceptions=True,
    )
    if not found:
        return set()

    dirty: set[int] = set()
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        accs = (
            await db.execute(
                select(PanelAccount).where(PanelAccount.id.in_(list(found.keys())))
            )
        ).scalars().all()
        by_id = {int(a.id): a for a in accs}
        for aid, (got, hit_name) in found.items():
            acc = by_id.get(int(aid))
            if acc is None:
                continue
            if hit_name and (acc.remote_username or "").strip() != hit_name:
                acc.remote_username = hit_name
            _apply_got_to_account(acc, int(got), now=now, dirty=dirty)
        await db.commit()
    logger.info(
        "heal-zero panel=%s tried=%s found=%s dirty=%s",
        pid,
        len(jobs),
        len(found),
        len(dirty),
    )
    return dirty


async def recount_users_usage(user_ids: list[int], *, concurrency: int = 6) -> list[int]:
    """Recompute weighted used_traffic + enforce quota. Returns flipped user ids."""
    from app.services.orchestrator import (
        activate_on_hold_if_used,
        enforce_user_quota,
        recount_user_used_traffic,
    )

    ids = list(dict.fromkeys(int(x) for x in user_ids if x))
    if not ids:
        return []
    sem = asyncio.Semaphore(max(1, concurrency))
    flipped: list[int] = []

    async def _one(uid: int) -> None:
        async with sem:
            try:
                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(User)
                        .where(User.id == uid)
                        .options(
                            selectinload(User.panel_accounts).selectinload(
                                PanelAccount.panel
                            ),
                            selectinload(User.groups),
                        )
                    )
                    user = result.scalar_one_or_none()
                    if user is None:
                        return
                    if user.status not in (
                        UserStatus.active,
                        UserStatus.on_hold,
                        UserStatus.limited,
                        UserStatus.expired,
                    ):
                        return
                    await recount_user_used_traffic(db, user)
                    activated = await activate_on_hold_if_used(db, user)
                    changed = bool(activated) or await enforce_user_quota(db, user)
                    await db.commit()
                    if changed or user.status in (
                        UserStatus.limited,
                        UserStatus.expired,
                        UserStatus.disabled,
                    ):
                        if user.status in (
                            UserStatus.limited,
                            UserStatus.expired,
                            UserStatus.disabled,
                        ):
                            flipped.append(int(user.id))
                        elif changed:
                            flipped.append(int(user.id))
            except Exception:  # noqa: BLE001
                logger.warning("recount failed user_id=%s", uid, exc_info=True)
                try:
                    async with AsyncSessionLocal() as db2:
                        result2 = await db2.execute(
                            select(User)
                            .where(User.id == uid)
                            .options(
                                selectinload(User.panel_accounts).selectinload(
                                    PanelAccount.panel
                                ),
                                selectinload(User.groups),
                            )
                        )
                        user2 = result2.scalar_one_or_none()
                        if user2 is None:
                            return
                        await recount_user_used_traffic(db2, user2)
                        await activate_on_hold_if_used(db2, user2)
                        await enforce_user_quota(db2, user2)
                        await db2.commit()
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "recount retry failed user_id=%s", uid, exc_info=True
                    )

    await asyncio.gather(*[_one(uid) for uid in ids])
    return list(dict.fromkeys(flipped))


async def sync_one_panel_bulk(
    panel_id: int, *, recount: bool = True, recount_concurrency: int = 24
) -> dict[str, int]:
    """Fetch /api/users for one panel, apply, heal zeros, recount dirty users."""
    t0 = time.monotonic()
    async with AsyncSessionLocal() as db:
        panel = (
            await db.execute(select(Panel).where(Panel.id == int(panel_id)))
        ).scalar_one_or_none()
        if panel is None or not panel.is_active or panel.type not in _BULK_TYPES:
            return {"accounts": 0, "dirty": 0, "flipped": 0, "listed": 0}
        pid = int(panel.id)
        panel_ref = panel
    usage_map = await fetch_panel_usage_map(panel_ref)
    dirty: set[int] = set()
    if usage_map:
        async with AsyncSessionLocal() as db:
            dirty = await apply_usage_map_to_panel(db, pid, usage_map)
            await db.commit()
    # Always try per-account heal for remotes still at 0 (alias miss / empty map)
    try:
        healed = await heal_zero_accounts_via_get_usage(
            panel_ref,
            limit=500 if panel_ref.type == PanelType.pasarguard else 250,
            concurrency=24 if panel_ref.type == PanelType.pasarguard else 16,
        )
        dirty |= healed
    except Exception:  # noqa: BLE001
        logger.warning("heal-zero panel=%s failed", pid, exc_info=True)

    if not usage_map and not dirty:
        return {"accounts": 0, "dirty": 0, "flipped": 0, "listed": 0}

    recount_ids = list(dict.fromkeys(int(x) for x in dirty))
    flipped: list[int] = []
    if recount and recount_ids:
        flipped = await recount_users_usage(
            recount_ids, concurrency=max(8, int(recount_concurrency))
        )
    try:
        from app.services import user_analytics as analytics_svc

        await analytics_svc.align_analytics_cursors(list(dirty))
    except Exception:  # noqa: BLE001
        logger.debug("analytics cursor align after bulk failed", exc_info=True)
    dt = time.monotonic() - t0
    logger.info(
        "bulk usage panel=%s listed=%s dirty=%s recount=%s flipped=%s dt=%.1fs",
        pid,
        len(usage_map),
        len(dirty),
        len(recount_ids) if recount else 0,
        len(flipped),
        dt,
    )
    return {
        "listed": len(usage_map),
        "dirty": len(dirty),
        "flipped": len(flipped),
        "dirty_ids": recount_ids,
        "panel_id": pid,
    }


async def sync_panels_bulk_rotate(*, max_panels: int = 3) -> dict[str, int]:
    """Round-robin bulk sync across Marzban-family panels."""
    global _ROTATE_CURSOR
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Panel.id)
                .where(
                    Panel.is_active.is_(True),
                    Panel.type.in_(tuple(_BULK_TYPES)),
                )
                .order_by(Panel.id.asc())
            )
        ).all()
    ids = [int(r[0]) for r in rows]
    if not ids:
        return {"panels": 0, "dirty": 0, "flipped": 0, "listed": 0}

    n = max(1, min(int(max_panels), len(ids)))
    start = _ROTATE_CURSOR % len(ids)
    _ROTATE_CURSOR = (start + n) % len(ids)
    chosen = [ids[(start + i) % len(ids)] for i in range(n)]

    listed = dirty = flipped = 0
    for pid in chosen:
        try:
            stats = await sync_one_panel_bulk(pid)
            listed += int(stats.get("listed") or 0)
            dirty += int(stats.get("dirty") or 0)
            flipped += int(stats.get("flipped") or 0)
        except Exception:  # noqa: BLE001
            logger.warning("bulk rotate panel=%s failed", pid, exc_info=True)
    return {
        "panels": len(chosen),
        "listed": listed,
        "dirty": dirty,
        "flipped": flipped,
    }


async def sync_all_panels_bulk(
    *,
    concurrency: int = 10,
    recount: bool = True,
    recount_concurrency: int = 28,
) -> dict[str, int]:
    """Pull usage from EVERY active Marzban-family panel in parallel."""
    t0 = time.monotonic()
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Panel.id)
                .where(
                    Panel.is_active.is_(True),
                    Panel.type.in_(tuple(_BULK_TYPES)),
                )
                .order_by(Panel.id.asc())
            )
        ).all()
    ids = [int(r[0]) for r in rows]
    if not ids:
        return {"panels": 0, "dirty": 0, "flipped": 0, "listed": 0, "dt": 0}

    sem = asyncio.Semaphore(max(1, int(concurrency)))
    listed = 0
    dirty_ids: set[int] = set()
    lock = asyncio.Lock()

    async def _one(pid: int) -> None:
        nonlocal listed
        async with sem:
            try:
                stats = await sync_one_panel_bulk(
                    pid, recount=False, recount_concurrency=recount_concurrency
                )
            except Exception:  # noqa: BLE001
                logger.warning("fleet bulk panel=%s failed", pid, exc_info=True)
                return
            async with lock:
                listed += int(stats.get("listed") or 0)
                for uid in stats.get("dirty_ids") or []:
                    dirty_ids.add(int(uid))

    await asyncio.gather(*[_one(pid) for pid in ids])

    flipped: list[int] = []
    if recount and dirty_ids:
        ordered = list(dirty_ids)
        chunk = 800
        for i in range(0, len(ordered), chunk):
            part = await recount_users_usage(
                ordered[i : i + chunk],
                concurrency=max(8, int(recount_concurrency)),
            )
            flipped.extend(part)

    dt = time.monotonic() - t0
    logger.info(
        "fleet bulk panels=%s listed=%s dirty=%s flipped=%s dt=%.1fs",
        len(ids),
        listed,
        len(dirty_ids),
        len(flipped),
        dt,
    )
    return {
        "panels": len(ids),
        "listed": listed,
        "dirty": len(dirty_ids),
        "flipped": len(list(dict.fromkeys(flipped))),
        "dt": int(dt),
    }


async def recount_all_live_users(
    *, concurrency: int = 28, chunk: int = 1000
) -> dict[str, int]:
    """Recompute weighted used_traffic for every live/limited user (DB only)."""
    t0 = time.monotonic()
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(User.id).where(
                    User.status.in_(
                        (
                            UserStatus.active,
                            UserStatus.on_hold,
                            UserStatus.limited,
                            UserStatus.expired,
                        )
                    )
                )
            )
        ).all()
    ids = [int(r[0]) for r in rows]
    flipped: list[int] = []
    step = max(100, int(chunk))
    for i in range(0, len(ids), step):
        flipped.extend(
            await recount_users_usage(
                ids[i : i + step], concurrency=max(8, int(concurrency))
            )
        )
    dt = time.monotonic() - t0
    logger.info(
        "fleet recount users=%s flipped=%s dt=%.1fs",
        len(ids),
        len(list(dict.fromkeys(flipped))),
        dt,
    )
    return {
        "users": len(ids),
        "flipped": len(list(dict.fromkeys(flipped))),
        "dt": int(dt),
    }


_FLEET_CYCLE_N = 0


async def run_fleet_usage_cycle() -> dict[str, int]:
    """Full-fleet usage: Astra Xray + all remote panels + dirty recount.

    Full recount of every user runs every 5th cycle so hot /sub users are not
    starved behind a multi-minute Σ over the whole install.
    """
    global _FLEET_CYCLE_N
    from app.services.orchestrator import sync_all_astra_traffic

    t0 = time.monotonic()
    _FLEET_CYCLE_N += 1
    # Full recount of every user is the heavy path — every 8th cycle only.
    full_pass = _FLEET_CYCLE_N % 8 == 1
    stats: dict[str, int] = {
        "astra_panels": 0,
        "panels": 0,
        "listed": 0,
        "dirty": 0,
        "flipped": 0,
        "recount_users": 0,
        "full_pass": int(full_pass),
    }
    try:
        stats["astra_panels"] = int(await sync_all_astra_traffic() or 0)
    except Exception:  # noqa: BLE001
        logger.warning("fleet astra sync failed", exc_info=True)

    try:
        bulk = await sync_all_panels_bulk(
            concurrency=8, recount=True, recount_concurrency=12
        )
        stats["panels"] = int(bulk.get("panels") or 0)
        stats["listed"] = int(bulk.get("listed") or 0)
        stats["dirty"] = int(bulk.get("dirty") or 0)
        stats["flipped"] += int(bulk.get("flipped") or 0)
    except Exception:  # noqa: BLE001
        logger.warning("fleet remote bulk failed", exc_info=True)

    if full_pass:
        try:
            full = await recount_all_live_users(concurrency=10, chunk=600)
            stats["recount_users"] = int(full.get("users") or 0)
            stats["flipped"] += int(full.get("flipped") or 0)
        except Exception:  # noqa: BLE001
            logger.warning("fleet full recount failed", exc_info=True)

    stats["dt"] = int(time.monotonic() - t0)
    logger.info(
        "fleet usage cycle astra=%s panels=%s listed=%s dirty=%s "
        "recount=%s full=%s flipped=%s dt=%ss",
        stats["astra_panels"],
        stats["panels"],
        stats["listed"],
        stats["dirty"],
        stats["recount_users"],
        full_pass,
        stats["flipped"],
        stats["dt"],
    )
    return stats


async def find_exhausted_user_ids(*, limit: int = 200) -> list[int]:
    """SQL: active users whose stored used_traffic already hit data_limit."""
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT id FROM users "
                "WHERE status = 'active' AND data_limit > 0 "
                "AND used_traffic >= data_limit "
                "ORDER BY used_traffic DESC LIMIT :lim"
            ),
            {"lim": max(1, int(limit))},
        )
        return [int(x) for (x,) in result.all()]


async def find_time_expired_user_ids(*, limit: int = 300) -> list[int]:
    """Active users whose expire unix timestamp has already passed."""
    import time

    from sqlalchemy import text

    now = int(time.time())
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT id FROM users "
                "WHERE status = 'active' "
                "AND expire IS NOT NULL AND expire > 0 AND expire <= :now "
                "ORDER BY expire ASC LIMIT :lim"
            ),
            {"lim": max(1, int(limit)), "now": now},
        )
        return [int(x) for (x,) in result.all()]


async def flip_time_expired_users(*, limit: int = 300) -> list[int]:
    """Mark due active users as expired. Returns flipped user ids."""
    import time

    from sqlalchemy import update

    from app.models.user import User, UserStatus

    now = int(time.time())
    ids = await find_time_expired_user_ids(limit=limit)
    if not ids:
        return []
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(User)
            .where(
                User.id.in_(ids),
                User.status == UserStatus.active,
                User.expire.is_not(None),
                User.expire > 0,
                User.expire <= now,
            )
            .values(status=UserStatus.expired)
        )
        await db.commit()
    return ids


async def find_limited_needing_freeze_ids(*, limit: int = 200) -> list[int]:
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT DISTINCT u.id FROM users u "
                "JOIN panel_accounts pa ON pa.user_id = u.id "
                "JOIN panels p ON p.id = pa.panel_id AND p.is_active IS TRUE "
                "WHERE u.status IN ('limited', 'expired') "
                "AND pa.status IN ('active', 'error', 'pending') "
                "AND COALESCE(pa.error_message, '') <> :mark "
                "ORDER BY CASE WHEN u.status = 'expired' THEN 0 ELSE 1 END, "
                "u.id DESC LIMIT :lim"
            ),
            {"lim": max(1, int(limit)), "mark": QUOTA_FROZEN_MARK},
        )
        return [int(x) for (x,) in result.all()]


async def find_expired_needing_freeze_ids(*, limit: int = 200) -> list[int]:
    """Expired users that still have unfrozen remotes on active panels."""
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT DISTINCT u.id FROM users u "
                "JOIN panel_accounts pa ON pa.user_id = u.id "
                "JOIN panels p ON p.id = pa.panel_id AND p.is_active IS TRUE "
                "WHERE u.status = 'expired' "
                "AND pa.status IN ('active', 'error', 'pending') "
                "AND COALESCE(pa.error_message, '') <> :mark "
                "ORDER BY u.expire ASC, u.id DESC LIMIT :lim"
            ),
            {"lim": max(1, int(limit)), "mark": QUOTA_FROZEN_MARK},
        )
        return [int(x) for (x,) in result.all()]


async def find_false_limited_user_ids(*, limit: int = 200) -> list[int]:
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT id FROM users "
                "WHERE status = 'limited' AND data_limit > 0 "
                "AND used_traffic < data_limit "
                "ORDER BY (data_limit - used_traffic) DESC LIMIT :lim"
            ),
            {"lim": max(1, int(limit))},
        )
        return [int(x) for (x,) in result.all()]


async def find_active_needing_unfreeze_ids(*, limit: int = 80) -> list[int]:
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT DISTINCT u.id FROM users u "
                "JOIN panel_accounts pa ON pa.user_id = u.id "
                "JOIN panels p ON p.id = pa.panel_id AND p.is_active IS TRUE "
                "WHERE u.status = 'active' "
                "AND u.data_limit > 0 AND u.used_traffic < u.data_limit "
                "AND pa.status IN ('active', 'error', 'pending') "
                "AND COALESCE(pa.error_message, '') = :mark "
                "ORDER BY u.id DESC LIMIT :lim"
            ),
            {"lim": max(1, int(limit)), "mark": QUOTA_FROZEN_MARK},
        )
        return [int(x) for (x,) in result.all()]


async def find_near_limit_user_ids(*, limit: int = 80) -> list[int]:
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT id FROM users "
                "WHERE status IN ('active','on_hold') AND data_limit > 0 "
                "AND used_traffic >= (data_limit * 35) / 100 "
                "ORDER BY (used_traffic::float / NULLIF(data_limit,0)) DESC "
                "LIMIT :lim"
            ),
            {"lim": max(1, int(limit))},
        )
        return [int(x) for (x,) in result.all()]
