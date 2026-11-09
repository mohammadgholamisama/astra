"""Collect and query per-panel analytics (raw usage, online, test quality)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.panel import Panel
from app.models.panel_analytics import (
    PanelTestRun,
    PanelUsageHourBucket,
    PanelUsageState,
    PanelUserTrafficState,
)
from app.models.user import PanelAccount, PanelAccountStatus
from app.models.user_analytics import SubClientProfile, SubFetchEvent, UserUsageState
from app.services.iran_operators import classify_operator, operator_label

logger = logging.getLogger("astra.panel_analytics")

_ONLINE_MIN_BYTES = 48 * 1024
_IDLE_STREAK_END = 2
_HOUR_RETENTION_DAYS = 120
_TEST_RETENTION_DAYS = 180
_sample_lock = asyncio.Lock()

# "Really good" thresholds from ping-rank metrics
_GOOD_PING_MS = 180.0
_GOOD_STABILITY = 70.0
_GOOD_PACKET_LOSS = 5.0

_BRAND_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("iPhone", re.compile(r"iphone", re.I)),
    ("iPad", re.compile(r"ipad", re.I)),
    ("Samsung", re.compile(r"samsung|\bsm-[a-z0-9]+", re.I)),
    ("Xiaomi", re.compile(r"xiaomi|\bredmi\b|\bpoco\b|\bmi\s?\d", re.I)),
    ("Huawei", re.compile(r"huawei|\bhuawei|\balphabet", re.I)),
    ("Honor", re.compile(r"\bhonor\b|\bhry-", re.I)),
    ("OnePlus", re.compile(r"oneplus|\bop\d{2}", re.I)),
    ("OPPO", re.compile(r"\boppo\b|\bcph\d+", re.I)),
    ("vivo", re.compile(r"\bvivo\b", re.I)),
    ("Realme", re.compile(r"realme", re.I)),
    ("Nokia", re.compile(r"nokia", re.I)),
    ("Google Pixel", re.compile(r"pixel", re.I)),
    ("Sony", re.compile(r"sony|xperia", re.I)),
    ("LG", re.compile(r"\blg[- ]", re.I)),
    ("Motorola", re.compile(r"motorola|\bmoto\b", re.I)),
    ("Windows", re.compile(r"windows nt|windows phone", re.I)),
    ("macOS", re.compile(r"mac os x|macintosh", re.I)),
    ("Linux", re.compile(r"linux(?!.*android)", re.I)),
    ("Android", re.compile(r"android", re.I)),
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _hour_ts(dt: datetime | None = None) -> int:
    d = dt or _utc_now()
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    d = d.astimezone(timezone.utc)
    return int(d.replace(minute=0, second=0, microsecond=0).timestamp())


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_device_brand(user_agent: str) -> str:
    ua = (user_agent or "").strip()
    if not ua:
        return "نامشخص"
    for name, pat in _BRAND_PATTERNS:
        if pat.search(ua):
            return name
    return "سایر"


def format_bps(bps: float) -> str:
    n = float(bps or 0)
    if n <= 0:
        return "۰"
    mbytes_s = n / 8.0 / (1024 * 1024)
    if mbytes_s >= 1:
        return f"{mbytes_s:.2f} MB/s"
    kbytes_s = n / 8.0 / 1024
    if kbytes_s >= 1:
        return f"{kbytes_s:.1f} KB/s"
    return f"{n / 8.0:.0f} B/s"


def _pct_change(cur: int | float, prev: int | float) -> float | None:
    p = float(prev or 0)
    c = float(cur or 0)
    if p <= 0:
        return None if c <= 0 else 100.0
    return round(((c - p) / p) * 100.0, 1)


async def _get_or_create_panel_state(db: AsyncSession, panel_id: int) -> PanelUsageState:
    st = await db.get(PanelUsageState, int(panel_id))
    if st is None:
        st = PanelUsageState(panel_id=int(panel_id), last_raw_total=0)
        db.add(st)
        await db.flush()
    return st


async def _bump_panel_hour(
    db: AsyncSession,
    panel_id: int,
    hour_ts: int,
    delta: int,
    active_seconds: int,
    peak_bps: float,
    online_n: int,
) -> None:
    row = (
        await db.execute(
            select(PanelUsageHourBucket).where(
                PanelUsageHourBucket.panel_id == int(panel_id),
                PanelUsageHourBucket.hour_ts == int(hour_ts),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        db.add(
            PanelUsageHourBucket(
                panel_id=int(panel_id),
                hour_ts=int(hour_ts),
                raw_bytes=max(0, int(delta)),
                active_seconds=max(0, int(active_seconds)),
                sample_count=1,
                peak_bps=max(0.0, float(peak_bps)),
                online_sum=max(0, int(online_n)),
                online_peak=max(0, int(online_n)),
            )
        )
        return
    row.raw_bytes = int(row.raw_bytes or 0) + max(0, int(delta))
    row.active_seconds = int(row.active_seconds or 0) + max(0, int(active_seconds))
    row.sample_count = int(row.sample_count or 0) + 1
    row.online_sum = int(row.online_sum or 0) + max(0, int(online_n))
    if int(online_n) > int(row.online_peak or 0):
        row.online_peak = int(online_n)
    if float(peak_bps) > float(row.peak_bps or 0):
        row.peak_bps = float(peak_bps)


async def apply_panel_raw_sample(
    db: AsyncSession,
    panel_id: int,
    raw_total: int,
    *,
    online_n: int = 0,
    now: datetime | None = None,
) -> None:
    now = _as_utc(now) or _utc_now()
    state = await _get_or_create_panel_state(db, panel_id)
    prev = int(state.last_raw_total or 0)
    raw = max(0, int(raw_total))
    last_at = _as_utc(state.last_sample_at)
    if last_at is None:
        state.last_raw_total = raw
        state.last_sample_at = now
        return

    dt = max(1.0, min(600.0, (now - last_at).total_seconds()))
    delta = 0 if raw < prev else (raw - prev)
    rate_bps = (delta * 8.0) / dt if delta > 0 else 0.0
    hour = _hour_ts(now)
    active_sec = int(round(dt)) if delta >= _ONLINE_MIN_BYTES else 0
    await _bump_panel_hour(
        db, panel_id, hour, delta, active_sec, rate_bps, int(online_n)
    )
    if rate_bps > float(state.peak_bps or 0):
        state.peak_bps = float(rate_bps)
        state.peak_bps_at = now
    state.last_raw_total = raw
    state.last_sample_at = now


async def apply_panel_user_sample(
    db: AsyncSession,
    user_id: int,
    panel_id: int,
    raw: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Update (user,panel) cursor; return True if currently online on this panel."""
    now = _as_utc(now) or _utc_now()
    row = (
        await db.execute(
            select(PanelUserTrafficState).where(
                PanelUserTrafficState.user_id == int(user_id),
                PanelUserTrafficState.panel_id == int(panel_id),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        db.add(
            PanelUserTrafficState(
                user_id=int(user_id),
                panel_id=int(panel_id),
                last_raw=max(0, int(raw)),
                last_sample_at=now,
                online_since=None,
                idle_streak=0,
            )
        )
        return False

    prev = int(row.last_raw or 0)
    cur = max(0, int(raw))
    last_at = _as_utc(row.last_sample_at)
    if last_at is None:
        row.last_raw = cur
        row.last_sample_at = now
        return False

    delta = 0 if cur < prev else (cur - prev)
    if delta >= _ONLINE_MIN_BYTES:
        if row.online_since is None:
            row.online_since = now
        row.idle_streak = 0
    else:
        if row.online_since is not None:
            row.idle_streak = int(row.idle_streak or 0) + 1
            if row.idle_streak >= _IDLE_STREAK_END:
                row.online_since = None
                row.idle_streak = 0
    row.last_raw = cur
    row.last_sample_at = now
    return row.online_since is not None


async def _sample_panels_with_db(db: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """Advance panel + user×panel cursors from current PanelAccount counters."""
    now = _as_utc(now) or _utc_now()
    up_rows = (
        await db.execute(
            select(
                PanelAccount.user_id,
                PanelAccount.panel_id,
                func.coalesce(func.sum(PanelAccount.used_traffic), 0),
            )
            .where(
                PanelAccount.status.in_(
                    (
                        PanelAccountStatus.active,
                        PanelAccountStatus.error,
                        PanelAccountStatus.pending,
                        PanelAccountStatus.deleted,
                    )
                )
            )
            .group_by(PanelAccount.user_id, PanelAccount.panel_id)
        )
    ).all()

    online_by_panel: dict[int, int] = {}
    raw_by_panel: dict[int, int] = {}
    n_up = 0
    for uid, pid, total in up_rows:
        uid_i, pid_i = int(uid), int(pid)
        raw_i = int(total or 0)
        raw_by_panel[pid_i] = raw_by_panel.get(pid_i, 0) + raw_i
        try:
            is_on = await apply_panel_user_sample(db, uid_i, pid_i, raw_i, now=now)
            if is_on:
                online_by_panel[pid_i] = online_by_panel.get(pid_i, 0) + 1
            n_up += 1
        except Exception:  # noqa: BLE001
            logger.exception("panel user sample u=%s p=%s failed", uid_i, pid_i)
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
        if n_up % 80 == 0:
            await db.commit()

    all_pids = {int(r[0]) for r in (await db.execute(select(Panel.id))).all()}
    for pid in all_pids:
        raw_by_panel.setdefault(int(pid), 0)
        online_by_panel.setdefault(int(pid), 0)

    n_p = 0
    for pid, raw in raw_by_panel.items():
        try:
            await apply_panel_raw_sample(
                db,
                int(pid),
                int(raw),
                online_n=int(online_by_panel.get(int(pid), 0)),
                now=now,
            )
            n_p += 1
        except Exception:  # noqa: BLE001
            logger.exception("panel sample p=%s failed", pid)
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
    await db.commit()
    return {"panels": n_p, "user_panel": n_up}


async def sample_all_panels_once(*, force: bool = False) -> dict[str, int]:
    """Sample raw traffic per panel + per (user,panel) online flags."""
    from app.core.database import AsyncSessionLocal

    if _sample_lock.locked() and not force:
        return {"skipped": 1}
    async with _sample_lock:
        t0 = time.monotonic()
        async with AsyncSessionLocal() as db:
            stats = await _sample_panels_with_db(db)
        stats["ms"] = int((time.monotonic() - t0) * 1000)
        return stats


async def live_panel_totals(db: AsyncSession) -> dict[int, int]:
    """Current Σ PanelAccount.used_traffic per panel (authoritative cumulative)."""
    rows = (
        await db.execute(
            select(
                PanelAccount.panel_id,
                func.coalesce(func.sum(PanelAccount.used_traffic), 0),
            )
            .where(
                PanelAccount.status.in_(
                    (
                        PanelAccountStatus.active,
                        PanelAccountStatus.error,
                        PanelAccountStatus.pending,
                        PanelAccountStatus.deleted,
                    )
                )
            )
            .group_by(PanelAccount.panel_id)
        )
    ).all()
    return {int(pid): int(raw or 0) for pid, raw in rows}


async def refresh_panels_live(*, pull_remotes: bool = True) -> dict[str, Any]:
    """Pull fresh remote usage then resample all panel analytics.

    This is what the admin «تازه‌سازی زنده» button should call so hour/day
    buckets and online flags catch up immediately.
    """
    from app.core.database import AsyncSessionLocal
    from app.models.panel import PanelType
    from app.models.user import User
    from app.services.orchestrator import refresh_users_usage_batch

    t0 = time.monotonic()
    pulled = 0
    astra_synced = 0

    async with _sample_lock:
        async with AsyncSessionLocal() as db:
            if pull_remotes:
                # 1) Fast path: Astra node local stats (no HTTP to foreign panels)
                try:
                    from app.services import astra_node as astra_node_svc

                    astra_panels = list(
                        (
                            await db.execute(
                                select(Panel).where(
                                    Panel.type == PanelType.astra,
                                    Panel.is_active.is_(True),
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for panel in astra_panels:
                        try:
                            n = await astra_node_svc.sync_traffic_from_xray(db, panel)
                            astra_synced += int(n or 0)
                        except Exception:  # noqa: BLE001
                            logger.debug(
                                "astra sync panel=%s failed", panel.id, exc_info=True
                            )
                    await db.commit()
                except Exception:  # noqa: BLE001
                    logger.warning("astra bulk sync for analytics failed", exc_info=True)
                    try:
                        await db.rollback()
                    except Exception:  # noqa: BLE001
                        pass

                # 2) Live-pull a capped set of users (newest with accounts first)
                try:
                    users = list(
                        (
                            await db.execute(
                                select(User)
                                .join(PanelAccount, PanelAccount.user_id == User.id)
                                .distinct()
                                .order_by(User.id.desc())
                                .limit(250)
                            )
                        )
                        .scalars()
                        .unique()
                        .all()
                    )
                    if users:
                        await refresh_users_usage_batch(
                            db,
                            users,
                            force=True,
                            concurrency=12,
                            push_remotes=False,
                            quick=True,
                            isolated=False,
                        )
                        await db.commit()
                        pulled = len(users)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "panel analytics remote pull failed", exc_info=True
                    )
                    try:
                        await db.rollback()
                    except Exception:  # noqa: BLE001
                        pass

            sample_stats = await _sample_panels_with_db(db)

    return {
        "ok": True,
        "users_pulled": pulled,
        "astra_synced": astra_synced,
        **sample_stats,
        "ms": int((time.monotonic() - t0) * 1000),
    }


async def prune_panel_analytics(db: AsyncSession) -> dict[str, int]:
    now = _utc_now()
    cut_hour = int((now - timedelta(days=_HOUR_RETENTION_DAYS)).timestamp())
    cut_test = now - timedelta(days=_TEST_RETENTION_DAYS)
    r1 = await db.execute(
        delete(PanelUsageHourBucket).where(PanelUsageHourBucket.hour_ts < cut_hour)
    )
    r2 = await db.execute(delete(PanelTestRun).where(PanelTestRun.ts < cut_test))
    await db.commit()
    return {
        "hour_buckets": int(r1.rowcount or 0),
        "test_runs": int(r2.rowcount or 0),
    }


def _is_good_config(raw: dict[str, Any]) -> bool:
    try:
        avg = float(raw.get("avg_ping") if raw.get("avg_ping") is not None else -1)
    except (TypeError, ValueError):
        avg = -1.0
    try:
        stab = float(raw.get("stability_pct") if raw.get("stability_pct") is not None else -1)
    except (TypeError, ValueError):
        stab = -1.0
    try:
        pkt = float(
            raw.get("packet_loss_pct") if raw.get("packet_loss_pct") is not None else -1
        )
    except (TypeError, ValueError):
        pkt = -1.0
    has_pkt = bool(raw.get("has_packet_loss"))
    try:
        dl = float(raw.get("download_kbps") or 0)
    except (TypeError, ValueError):
        dl = 0.0
    if avg <= 0 and dl <= 0 and stab <= 0:
        return False
    if avg > 0 and avg > _GOOD_PING_MS:
        return False
    if stab > 0 and stab < _GOOD_STABILITY:
        return False
    if has_pkt and pkt >= 0 and pkt > _GOOD_PACKET_LOSS:
        return False
    return True


def _all_ops_ok(raw: dict[str, Any]) -> bool:
    """True when every known operator has a positive ping sample."""
    ops = raw.get("operator_pings") or raw.get("operators") or []
    if not isinstance(ops, list) or not ops:
        # Fallback: multiple devices often means multi-network coverage
        try:
            return int(raw.get("device_count") or 0) >= 2 and float(
                raw.get("avg_ping") or -1
            ) > 0
        except (TypeError, ValueError):
            return False
    seen: set[str] = set()
    ok: set[str] = set()
    for item in ops:
        if not isinstance(item, dict):
            continue
        code = str(item.get("operator") or item.get("op") or "").lower().strip()
        if code not in ("irancell", "mci", "rightel"):
            continue
        seen.add(code)
        try:
            ping = float(item.get("ping_ms") if item.get("ping_ms") is not None else -1)
        except (TypeError, ValueError):
            ping = -1.0
        has = bool(item.get("has_sample", ping > 0))
        if has and ping > 0:
            ok.add(code)
    if len(seen) >= 3:
        return len(ok) >= 3
    # If hub only sent subset, require all present ones OK and at least 2
    return len(seen) >= 2 and seen == ok


async def record_ping_rank_for_panels(
    db: AsyncSession,
    ranked: list[dict[str, Any]],
    *,
    workspace_items: list[Any] | None = None,
) -> dict[str, int]:
    """After apply_ping_rank: attribute scored configs to panels + store runs."""
    from app.services.config_orders import list_workspace

    if not ranked:
        return {"panels": 0}

    # Build fingerprint → panel names from workspace samples
    if workspace_items is None:
        ws = await list_workspace(db)
        workspace_items = list(ws.items)

    fp_panels: dict[tuple[str, int, str], list[str]] = {}
    for it in workspace_items:
        host = str(getattr(it, "host", "") or "").lower().strip()
        try:
            port = int(getattr(it, "port", 0) or 0)
        except (TypeError, ValueError):
            continue
        match = str(getattr(it, "match_host", "") or "").lower().strip()
        names = list(getattr(it, "panel_names", None) or [])
        if host and port > 0 and names:
            fp_panels[(host, port, match)] = [str(n) for n in names if n]

    panels = {
        str(p.name): int(p.id)
        for p in (await db.execute(select(Panel))).scalars().all()
    }

    # Global quality order among scored items (for top-100)
    scored: list[tuple[float, dict[str, Any]]] = []
    for raw in ranked:
        try:
            avg = float(raw.get("avg_ping") if raw.get("avg_ping") is not None else -1)
        except (TypeError, ValueError):
            avg = -1.0
        try:
            dl = float(raw.get("download_kbps") or 0)
        except (TypeError, ValueError):
            dl = 0.0
        if not (avg > 0 or dl > 0 or _is_good_config(raw)):
            # still count tested if any positive metric
            try:
                stab = float(raw.get("stability_pct") or -1)
            except (TypeError, ValueError):
                stab = -1.0
            if avg <= 0 and dl <= 0 and stab <= 0:
                continue
        # Lower avg_ping is better; unscored go last
        sort_key = avg if avg > 0 else 1e9
        scored.append((sort_key, raw))
    scored.sort(key=lambda x: x[0])
    top_keys = set()
    for i, (_, raw) in enumerate(scored[:100]):
        host = str(raw.get("host") or "").lower().strip()
        try:
            port = int(raw.get("port") or 0)
        except (TypeError, ValueError):
            continue
        match = str(raw.get("match_host") or "").lower().strip()
        top_keys.add((host, port, match))
        top_keys.add((host, port, ""))

    # Aggregate per panel
    agg: dict[int, dict[str, Any]] = {}
    for rank_i, (sort_key, raw) in enumerate(scored):
        host = str(raw.get("host") or "").lower().strip()
        try:
            port = int(raw.get("port") or 0)
        except (TypeError, ValueError):
            continue
        match = str(raw.get("match_host") or "").lower().strip()
        names = (
            fp_panels.get((host, port, match))
            or fp_panels.get((host, port, ""))
            or []
        )
        if not names:
            continue
        in_top = (host, port, match) in top_keys or (host, port, "") in top_keys
        good = _is_good_config(raw)
        all_ok = _all_ops_ok(raw)
        try:
            avg = float(raw.get("avg_ping") if raw.get("avg_ping") is not None else -1)
        except (TypeError, ValueError):
            avg = -1.0
        for name in names:
            pid = panels.get(name)
            if pid is None:
                continue
            a = agg.setdefault(
                pid,
                {
                    "tested": 0,
                    "in_top100": 0,
                    "good": 0,
                    "all_ops_ok": 0,
                    "ping_sum": 0.0,
                    "ping_n": 0,
                },
            )
            a["tested"] += 1
            if in_top:
                a["in_top100"] += 1
            if good:
                a["good"] += 1
            if all_ok:
                a["all_ops_ok"] += 1
            if avg > 0:
                a["ping_sum"] += avg
                a["ping_n"] += 1

    now = _utc_now()
    touched = 0
    for pid, a in agg.items():
        st = await _get_or_create_panel_state(db, pid)
        st.tests_total = int(st.tests_total or 0) + 1
        st.configs_tested_total = int(st.configs_tested_total or 0) + int(a["tested"])
        st.top100_total = int(st.top100_total or 0) + int(a["in_top100"])
        st.good_total = int(st.good_total or 0) + int(a["good"])
        st.all_ops_ok_total = int(st.all_ops_ok_total or 0) + int(a["all_ops_ok"])
        st.last_test_at = now
        avg_ping = (a["ping_sum"] / a["ping_n"]) if a["ping_n"] else -1.0
        db.add(
            PanelTestRun(
                panel_id=int(pid),
                ts=now,
                tested=int(a["tested"]),
                in_top100=int(a["in_top100"]),
                good=int(a["good"]),
                all_ops_ok=int(a["all_ops_ok"]),
                avg_ping=float(avg_ping),
                detail={
                    "scored_global": len(scored),
                    "rank_hint": "lower_avg_ping_first",
                },
            )
        )
        touched += 1
    await db.flush()
    logger.info("panel test analytics recorded panels=%s scored=%s", touched, len(scored))
    return {"panels": touched, "scored": len(scored)}


async def _sum_panel_hours(
    db: AsyncSession, panel_id: int, start_ts: int, end_ts: int
) -> int:
    q = await db.execute(
        select(func.coalesce(func.sum(PanelUsageHourBucket.raw_bytes), 0)).where(
            PanelUsageHourBucket.panel_id == int(panel_id),
            PanelUsageHourBucket.hour_ts >= int(start_ts),
            PanelUsageHourBucket.hour_ts < int(end_ts),
        )
    )
    return int(q.scalar() or 0)


async def _hour_rows_panel(
    db: AsyncSession, panel_id: int, start_ts: int, end_ts: int
) -> list[PanelUsageHourBucket]:
    res = await db.execute(
        select(PanelUsageHourBucket)
        .where(
            PanelUsageHourBucket.panel_id == int(panel_id),
            PanelUsageHourBucket.hour_ts >= int(start_ts),
            PanelUsageHourBucket.hour_ts < int(end_ts),
        )
        .order_by(PanelUsageHourBucket.hour_ts.asc())
    )
    return list(res.scalars().all())


async def _online_breakdown(db: AsyncSession) -> dict[str, Any]:
    """Online users by panel, operator, device brand."""
    online_rows = list(
        (
            await db.execute(
                select(PanelUserTrafficState).where(
                    PanelUserTrafficState.online_since.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )
    by_panel: dict[int, set[int]] = {}
    online_uids: set[int] = set()
    for r in online_rows:
        by_panel.setdefault(int(r.panel_id), set()).add(int(r.user_id))
        online_uids.add(int(r.user_id))

    # Global online (user-level) also counted
    global_online = {
        int(s.user_id)
        for s in (
            await db.execute(
                select(UserUsageState).where(UserUsageState.online_since.is_not(None))
            )
        ).scalars().all()
    }
    all_online = online_uids | global_online

    # Last fetch meta per user
    op_by_user: dict[int, str] = {}
    brand_by_user: dict[int, str] = {}
    if all_online:
        # Latest sub fetch per user
        events = (
            await db.execute(
                select(SubFetchEvent)
                .where(SubFetchEvent.user_id.in_(list(all_online)))
                .order_by(SubFetchEvent.ts.desc())
            )
        ).scalars().all()
        for e in events:
            uid = int(e.user_id)
            if uid not in op_by_user:
                op = getattr(e, "operator", None) or classify_operator(e.client_ip)
                op_by_user[uid] = op
            if uid not in brand_by_user:
                brand = getattr(e, "device_brand", None) or parse_device_brand(
                    e.user_agent or ""
                )
                brand_by_user[uid] = brand
        # Brand profiles fallback
        brands = (
            await db.execute(
                select(SubClientProfile).where(
                    SubClientProfile.user_id.in_(list(all_online)),
                    SubClientProfile.kind == "brand",
                )
            )
        ).scalars().all()
        for p in brands:
            uid = int(p.user_id)
            if uid not in brand_by_user:
                brand_by_user[uid] = p.label or "نامشخص"

    def _count_map(uids: set[int], mapping: dict[int, str], default: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for uid in uids:
            key = mapping.get(uid) or default
            out[key] = out.get(key, 0) + 1
        return out

    panels_out = []
    for pid, uids in sorted(by_panel.items(), key=lambda kv: -len(kv[1])):
        op_c = _count_map(uids, op_by_user, "other")
        panels_out.append(
            {
                "panel_id": pid,
                "online": len(uids),
                "operators": {
                    code: {
                        "count": n,
                        "label": operator_label(code),
                    }
                    for code, n in sorted(op_c.items(), key=lambda x: -x[1])
                },
                "brands": _count_map(uids, brand_by_user, "نامشخص"),
            }
        )

    global_ops = _count_map(all_online, op_by_user, "other")
    global_brands = _count_map(all_online, brand_by_user, "نامشخص")
    return {
        "total_online": len(all_online),
        "panel_online_total": sum(len(v) for v in by_panel.values()),
        "by_panel": panels_out,
        "operators": {
            code: {"count": n, "label": operator_label(code)}
            for code, n in sorted(global_ops.items(), key=lambda x: -x[1])
        },
        "brands": dict(sorted(global_brands.items(), key=lambda x: -x[1])),
    }


async def build_panel_overview(db: AsyncSession) -> dict[str, Any]:
    now = _utc_now()
    hour = _hour_ts(now)
    day = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    wd = now.weekday()
    week = int(
        (now - timedelta(days=wd))
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month = int(month_start.timestamp())
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)

    panels = list((await db.execute(select(Panel).order_by(Panel.id.asc()))).scalars().all())
    states = {
        int(s.panel_id): s
        for s in (await db.execute(select(PanelUsageState))).scalars().all()
    }
    # Live cumulative from accounts — always accurate even before hour buckets fill
    live_totals = await live_panel_totals(db)
    accounts_by_panel: dict[int, int] = {}
    for pid, cnt in (
        await db.execute(
            select(PanelAccount.panel_id, func.count())
            .where(
                PanelAccount.status.in_(
                    (
                        PanelAccountStatus.active,
                        PanelAccountStatus.error,
                        PanelAccountStatus.pending,
                    )
                )
            )
            .group_by(PanelAccount.panel_id)
        )
    ).all():
        accounts_by_panel[int(pid)] = int(cnt or 0)

    online_info = await _online_breakdown(db)
    online_by_pid = {
        int(x["panel_id"]): int(x["online"]) for x in online_info["by_panel"]
    }

    async def period_sum(pid: int, a: int, b: int) -> int:
        return await _sum_panel_hours(db, pid, a, b)

    rows = []
    total_hour = 0
    total_day = 0
    total_live = 0
    for p in panels:
        pid = int(p.id)
        st = states.get(pid)
        live = int(live_totals.get(pid, 0))
        total_live += live
        h = await period_sum(pid, hour, hour + 3600)
        d = await period_sum(pid, day, day + 86400)
        w = await period_sum(pid, week, week + 7 * 86400)
        m = await period_sum(pid, month, int(next_month.timestamp()))
        prev_h = await period_sum(pid, hour - 3600, hour)
        online_n = online_by_pid.get(pid, 0)
        peak = float(st.peak_bps or 0) if st else 0.0
        hour_bps = (h * 8.0) / 3600.0 if h > 0 else 0.0
        per_online_hour = (h / online_n) if online_n > 0 else None
        per_online_bps = (hour_bps / online_n) if online_n > 0 else None
        total_hour += h
        total_day += d
        last_run = (
            await db.execute(
                select(PanelTestRun)
                .where(PanelTestRun.panel_id == pid)
                .order_by(PanelTestRun.ts.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        rows.append(
            {
                "panel_id": pid,
                "name": p.name,
                "type": getattr(p.type, "value", p.type),
                "is_active": bool(getattr(p, "is_active", True)),
                "accounts": accounts_by_panel.get(pid, 0),
                "online": online_n,
                "raw": {
                    "current_total": live,
                    "hour": h,
                    "day": d,
                    "week": w,
                    "month": m,
                    "lifetime_cursor": int(st.last_raw_total or 0) if st else 0,
                },
                "compare_hour_pct": _pct_change(h, prev_h),
                "rate": {
                    "hour_bps": hour_bps,
                    "hour_label": format_bps(hour_bps),
                    "peak_bps": peak,
                    "peak_label": format_bps(peak),
                    "per_online_bytes_hour": per_online_hour,
                    "per_online_bps": per_online_bps,
                    "per_online_label": format_bps(per_online_bps or 0)
                    if per_online_bps
                    else None,
                },
                "tests": {
                    "runs": int(st.tests_total or 0) if st else 0,
                    "configs_tested": int(st.configs_tested_total or 0) if st else 0,
                    "top100": int(st.top100_total or 0) if st else 0,
                    "good": int(st.good_total or 0) if st else 0,
                    "all_ops_ok": int(st.all_ops_ok_total or 0) if st else 0,
                    "last_test_at": st.last_test_at.isoformat()
                    if st and st.last_test_at
                    else None,
                    "last_run": {
                        "tested": int(last_run.tested or 0),
                        "in_top100": int(last_run.in_top100 or 0),
                        "good": int(last_run.good or 0),
                        "all_ops_ok": int(last_run.all_ops_ok or 0),
                        "avg_ping": float(last_run.avg_ping or -1),
                        "ts": last_run.ts.isoformat() if last_run.ts else None,
                    }
                    if last_run
                    else None,
                },
            }
        )

    # Prefer live cumulative so panels with real usage never look "empty"
    rows.sort(
        key=lambda r: (
            -int(r["raw"]["current_total"] or 0),
            -int(r["raw"]["hour"] or 0),
            -int(r["online"] or 0),
            r["name"],
        )
    )

    # Global hourly 24h
    g_start = hour - 23 * 3600
    g_rows = (
        await db.execute(
            select(
                PanelUsageHourBucket.hour_ts,
                func.coalesce(func.sum(PanelUsageHourBucket.raw_bytes), 0),
            )
            .where(PanelUsageHourBucket.hour_ts >= g_start)
            .group_by(PanelUsageHourBucket.hour_ts)
            .order_by(PanelUsageHourBucket.hour_ts.asc())
        )
    ).all()
    g_map = {int(t): int(v or 0) for t, v in g_rows}
    global_hourly = [
        {"hour_ts": g_start + i * 3600, "raw_bytes": g_map.get(g_start + i * 3600, 0)}
        for i in range(24)
    ]

    # Peak hour-of-day last 7d across panels
    heat_start = hour - 7 * 86400
    heat = (
        await db.execute(
            select(PanelUsageHourBucket.hour_ts, PanelUsageHourBucket.raw_bytes).where(
                PanelUsageHourBucket.hour_ts >= heat_start
            )
        )
    ).all()
    by_hod = [0] * 24
    for ts, raw in heat:
        by_hod[int((int(ts) % 86400) // 3600)] += int(raw or 0)
    peak_hod = max(range(24), key=lambda h: by_hod[h]) if any(by_hod) else None

    return {
        "generated_at": now.isoformat(),
        "online": online_info,
        "totals": {
            "raw_current": total_live,
            "raw_hour": total_hour,
            "raw_day": total_day,
            "panels": len(panels),
        },
        "global_hourly": global_hourly,
        "hour_of_day": {
            "bytes": by_hod,
            "peak_hour": peak_hod,
            "peak_bytes": by_hod[peak_hod] if peak_hod is not None else 0,
        },
        "panels": rows,
        "note": (
            "مصرف فعلی = جمع خام اکانت‌های همان پنل (همیشه دقیق). "
            "ساعت/روز/هفته از نمونه‌برداری دوره‌ای است — با «تازه‌سازی زنده» تازه می‌شود."
        ),
    }


async def build_panel_detail(db: AsyncSession, panel_id: int) -> dict[str, Any]:
    panel = await db.get(Panel, int(panel_id))
    if panel is None:
        raise KeyError("panel")
    now = _utc_now()
    hour = _hour_ts(now)
    day = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    wd = now.weekday()
    week = int(
        (now - timedelta(days=wd))
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month = int(month_start.timestamp())
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)
    prev_month_start = (month_start - timedelta(days=1)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )

    st = await db.get(PanelUsageState, int(panel_id))
    live_map = await live_panel_totals(db)
    live_total = int(live_map.get(int(panel_id), 0))
    cur_h = await _sum_panel_hours(db, panel_id, hour, hour + 3600)
    prev_h = await _sum_panel_hours(db, panel_id, hour - 3600, hour)
    cur_d = await _sum_panel_hours(db, panel_id, day, day + 86400)
    prev_d = await _sum_panel_hours(db, panel_id, day - 86400, day)
    cur_w = await _sum_panel_hours(db, panel_id, week, week + 7 * 86400)
    prev_w = await _sum_panel_hours(db, panel_id, week - 7 * 86400, week)
    cur_m = await _sum_panel_hours(db, panel_id, month, int(next_month.timestamp()))
    prev_m = await _sum_panel_hours(
        db, panel_id, int(prev_month_start.timestamp()), month
    )

    series_start = hour - 47 * 3600
    buckets = await _hour_rows_panel(db, panel_id, series_start, hour + 3600)
    by_h = {int(b.hour_ts): b for b in buckets}
    hourly = []
    for i in range(48):
        ts = series_start + i * 3600
        b = by_h.get(ts)
        raw = int(b.raw_bytes or 0) if b else 0
        sc = int(b.sample_count or 0) if b else 0
        on_sum = int(b.online_sum or 0) if b else 0
        avg_online = (on_sum / sc) if sc else 0.0
        bps = (raw * 8.0) / 3600.0 if raw else 0.0
        per_on = (bps / avg_online) if avg_online > 0 else 0.0
        hourly.append(
            {
                "hour_ts": ts,
                "raw_bytes": raw,
                "peak_bps": float(b.peak_bps or 0) if b else 0.0,
                "avg_bps": bps,
                "avg_mbytes_s": round(raw / 3600 / (1024 * 1024), 4) if raw else 0.0,
                "avg_online": round(avg_online, 2),
                "online_peak": int(b.online_peak or 0) if b else 0,
                "per_online_bps": per_on,
                "per_online_label": format_bps(per_on),
            }
        )

    heat_start = hour - 14 * 86400
    heat_rows = await _hour_rows_panel(db, panel_id, heat_start, hour + 3600)
    by_hod = [0] * 24
    for b in heat_rows:
        by_hod[int((int(b.hour_ts) % 86400) // 3600)] += int(b.raw_bytes or 0)
    peak_hod = max(range(24), key=lambda h: by_hod[h]) if any(by_hod) else None

    online_info = await _online_breakdown(db)
    panel_online = next(
        (x for x in online_info["by_panel"] if int(x["panel_id"]) == int(panel_id)),
        {"online": 0, "operators": {}, "brands": {}},
    )

    tests = list(
        (
            await db.execute(
                select(PanelTestRun)
                .where(PanelTestRun.panel_id == int(panel_id))
                .order_by(PanelTestRun.ts.desc())
                .limit(30)
            )
        )
        .scalars()
        .all()
    )

    accounts_n = int(
        (
            await db.execute(
                select(func.count())
                .select_from(PanelAccount)
                .where(
                    PanelAccount.panel_id == int(panel_id),
                    PanelAccount.status.in_(
                        (
                            PanelAccountStatus.active,
                            PanelAccountStatus.error,
                            PanelAccountStatus.pending,
                        )
                    ),
                )
            )
        ).scalar()
        or 0
    )

    return {
        "panel_id": int(panel.id),
        "name": panel.name,
        "type": getattr(panel.type, "value", panel.type),
        "is_active": bool(getattr(panel, "is_active", True)),
        "accounts": accounts_n,
        "online": panel_online,
        "peak_bps": float(st.peak_bps or 0) if st else 0.0,
        "peak_bps_label": format_bps(float(st.peak_bps or 0) if st else 0),
        "peak_bps_at": st.peak_bps_at.isoformat() if st and st.peak_bps_at else None,
        "lifetime_raw": int(st.last_raw_total or 0) if st else 0,
        "current_total": live_total,
        "compare": {
            "hour": {
                "current": cur_h,
                "previous": prev_h,
                "change_pct": _pct_change(cur_h, prev_h),
            },
            "day": {
                "current": cur_d,
                "previous": prev_d,
                "change_pct": _pct_change(cur_d, prev_d),
            },
            "week": {
                "current": cur_w,
                "previous": prev_w,
                "change_pct": _pct_change(cur_w, prev_w),
            },
            "month": {
                "current": cur_m,
                "previous": prev_m,
                "change_pct": _pct_change(cur_m, prev_m),
            },
        },
        "hourly": hourly,
        "hour_of_day": {
            "bytes": by_hod,
            "peak_hour": peak_hod,
            "peak_bytes": by_hod[peak_hod] if peak_hod is not None else 0,
        },
        "tests": {
            "runs": int(st.tests_total or 0) if st else 0,
            "configs_tested": int(st.configs_tested_total or 0) if st else 0,
            "top100": int(st.top100_total or 0) if st else 0,
            "good": int(st.good_total or 0) if st else 0,
            "all_ops_ok": int(st.all_ops_ok_total or 0) if st else 0,
            "history": [
                {
                    "ts": t.ts.isoformat() if t.ts else None,
                    "tested": int(t.tested or 0),
                    "in_top100": int(t.in_top100 or 0),
                    "good": int(t.good or 0),
                    "all_ops_ok": int(t.all_ops_ok or 0),
                    "avg_ping": float(t.avg_ping or -1),
                }
                for t in tests
            ],
        },
        "rate_now": {
            "hour_bps": (cur_h * 8.0) / 3600.0,
            "hour_label": format_bps((cur_h * 8.0) / 3600.0),
            "per_online_bps": (
                ((cur_h * 8.0) / 3600.0) / int(panel_online.get("online") or 1)
            )
            if int(panel_online.get("online") or 0) > 0
            else 0.0,
        },
    }
