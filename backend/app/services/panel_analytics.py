"""Collect and query per-panel analytics (raw usage, online, test quality)."""

from __future__ import annotations

import asyncio
import logging
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
from app.models.user_analytics import UserUsageState

logger = logging.getLogger("astra.panel_analytics")

# Any meaningful traffic growth between samples (~60s) marks the user online.
_ONLINE_MIN_BYTES = 4 * 1024
_IDLE_STREAK_END = 3
_HOUR_RETENTION_DAYS = 120
_TEST_RETENTION_DAYS = 180
_sample_lock = asyncio.Lock()

# "Really good" thresholds from ping-rank metrics
_GOOD_PING_MS = 180.0
_GOOD_STABILITY = 70.0
_GOOD_PACKET_LOSS = 5.0


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


def _clamp_tz_offset(tz_offset_min: int | None) -> int:
    try:
        off = int(tz_offset_min or 0)
    except (TypeError, ValueError):
        off = 0
    return max(-14 * 60, min(14 * 60, off))


def _align_down_hour(ts: int) -> int:
    return int(ts) - (int(ts) % 3600)


def _align_up_hour(ts: int) -> int:
    t = int(ts)
    r = t % 3600
    return t if r == 0 else t + (3600 - r)


def _period_starts(now: datetime, tz_offset_min: int = 0) -> dict[str, int]:
    off = _clamp_tz_offset(tz_offset_min)
    local = now - timedelta(minutes=off)
    local_hour = local.replace(minute=0, second=0, microsecond=0)
    local_day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    wd = local.weekday()
    local_week = (local - timedelta(days=wd)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    local_month = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if local_month.month == 12:
        next_month = local_month.replace(year=local_month.year + 1, month=1)
    else:
        next_month = local_month.replace(month=local_month.month + 1)
    prev_month = (local_month - timedelta(days=1)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )

    def to_utc_ts(local_dt: datetime) -> int:
        return int((local_dt + timedelta(minutes=off)).timestamp())

    hour = to_utc_ts(local_hour)
    day = to_utc_ts(local_day)
    week = to_utc_ts(local_week)
    month = to_utc_ts(local_month)
    return {
        "hour": hour,
        "hour_end": hour + 3600,
        "day": day,
        "day_end": day + 86400,
        "week": week,
        "week_end": week + 7 * 86400,
        "month": month,
        "next_month": to_utc_ts(next_month),
        "prev_month": to_utc_ts(prev_month),
        "grid_hour": _align_down_hour(int(now.timestamp())),
        "tz_offset_min": off,
    }


def _hod_index(hour_ts: int, tz_offset_min: int = 0) -> int:
    off = _clamp_tz_offset(tz_offset_min)
    local_ts = int(hour_ts) - off * 60
    return int((local_ts % 86400) // 3600)


def _overlap_bytes(hour_ts: int, raw: int, start_ts: int, end_ts: int) -> int:
    if raw <= 0 or end_ts <= start_ts:
        return 0
    b0 = int(hour_ts)
    b1 = b0 + 3600
    o0 = max(b0, int(start_ts))
    o1 = min(b1, int(end_ts))
    if o1 <= o0:
        return 0
    if o0 == b0 and o1 == b1:
        return int(raw)
    return int(round(int(raw) * ((o1 - o0) / 3600.0)))


def _pending_bytes(*, live: int, cursor: int, seeded: bool) -> int:
    if not seeded:
        return 0
    return max(0, int(live) - int(cursor))


def _period_from_live_mark(
    live: int,
    *,
    mark_ts: int | None,
    mark_raw: int | None,
    period_ts: int,
) -> tuple[int, int, int]:
    """Authoritative bytes in the current local period from a live counter.

    Returns (bytes_in_period, new_mark_ts, new_mark_raw).
    On period rollover or counter reset, the mark is re-seeded to ``live``
    so we never dump lifetime into «امروز» / «این ساعت».
    """
    live_i = max(0, int(live or 0))
    period_i = int(period_ts)
    prev_ts = int(mark_ts or 0)
    prev_raw = max(0, int(mark_raw or 0))
    if prev_ts != period_i or live_i < prev_raw:
        return 0, period_i, live_i
    return live_i - prev_raw, period_i, prev_raw


def apply_live_period_marks(
    state: PanelUsageState,
    live: int,
    *,
    hour_ts: int,
    day_ts: int,
) -> tuple[int, int]:
    """Update hour/day watermarks on ``state``; return (hour_bytes, day_bytes)."""
    h, state.period_hour_ts, state.period_hour_raw = _period_from_live_mark(
        live,
        mark_ts=int(getattr(state, "period_hour_ts", 0) or 0),
        mark_raw=int(getattr(state, "period_hour_raw", 0) or 0),
        period_ts=hour_ts,
    )
    d, state.period_day_ts, state.period_day_raw = _period_from_live_mark(
        live,
        mark_ts=int(getattr(state, "period_day_ts", 0) or 0),
        mark_raw=int(getattr(state, "period_day_raw", 0) or 0),
        period_ts=day_ts,
    )
    # Day must include this hour; never show day < hour
    if d < h:
        d = h
    return int(h), int(d)


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


async def _distribute_panel_delta(
    db: AsyncSession,
    panel_id: int,
    delta: int,
    start: datetime,
    end: datetime,
    *,
    peak_bps: float = 0.0,
    online_n: int = 0,
    active: bool = False,
) -> None:
    if delta <= 0:
        return
    start = _as_utc(start) or end
    end = _as_utc(end) or start
    if end <= start:
        await _bump_panel_hour(
            db, panel_id, _hour_ts(end), delta, 0, peak_bps, int(online_n)
        )
        return
    total_sec = (end - start).total_seconds()
    if total_sec > 48 * 3600:
        start = end - timedelta(hours=48)
        total_sec = (end - start).total_seconds()
    segs: list[tuple[int, float]] = []
    t = start
    while t < end:
        hour_floor = _hour_ts(t)
        hour_end = datetime.fromtimestamp(hour_floor + 3600, tz=timezone.utc)
        seg_end = min(end, hour_end)
        segs.append((hour_floor, max(0.0, (seg_end - t).total_seconds())))
        t = seg_end
    if not segs:
        await _bump_panel_hour(
            db, panel_id, _hour_ts(end), delta, 0, peak_bps, int(online_n)
        )
        return
    assigned = 0
    for i, (h, sec) in enumerate(segs):
        if i == len(segs) - 1:
            part = max(0, delta - assigned)
        else:
            part = int(round(delta * (sec / total_sec))) if total_sec > 0 else 0
            assigned += part
        act = int(round(sec)) if active else 0
        if part > 0 or act > 0:
            await _bump_panel_hour(
                db, panel_id, h, part, act, peak_bps, int(online_n)
            )


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

    dt = max(1.0, (now - last_at).total_seconds())
    dt_rate = max(1.0, min(600.0, dt))
    delta = 0 if raw < prev else (raw - prev)
    rate_bps = (delta * 8.0) / dt_rate if delta > 0 else 0.0
    active = delta >= _ONLINE_MIN_BYTES
    await _distribute_panel_delta(
        db,
        panel_id,
        delta,
        last_at,
        now,
        peak_bps=rate_bps,
        online_n=int(online_n),
        active=active,
    )
    if rate_bps > float(state.peak_bps or 0):
        state.peak_bps = float(rate_bps)
        state.peak_bps_at = now
    state.last_raw_total = raw
    state.last_sample_at = now
    # Keep chart-hour watermark aligned with the sample cursor so overview
    # never re-seeds mid-hour to the full lifetime total.
    chart_h = _hour_ts(now)
    if int(getattr(state, "period_hour_ts", 0) or 0) != chart_h:
        state.period_hour_ts = chart_h
        state.period_hour_raw = raw - delta if delta > 0 else raw
    elif delta > 0:
        # mark stays; implied hour bytes grow with live
        pass


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
            async with db.begin_nested():
                is_on = await apply_panel_user_sample(db, uid_i, pid_i, raw_i, now=now)
            if is_on:
                online_by_panel[pid_i] = online_by_panel.get(pid_i, 0) + 1
            n_up += 1
        except Exception:  # noqa: BLE001
            logger.exception("panel user sample u=%s p=%s failed", uid_i, pid_i)
        if n_up % 80 == 0:
            await db.commit()

    all_pids = {int(r[0]) for r in (await db.execute(select(Panel.id))).all()}
    for pid in all_pids:
        raw_by_panel.setdefault(int(pid), 0)
        online_by_panel.setdefault(int(pid), 0)

    n_p = 0
    for pid, raw in raw_by_panel.items():
        try:
            async with db.begin_nested():
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
    await db.commit()
    return {"panels": n_p, "user_panel": n_up}


async def sample_all_panels_once(*, force: bool = False) -> dict[str, int]:
    """Sample raw traffic per panel + per (user,panel) online flags.

    Never waits on a busy lock so HTTP overview stays responsive.
    """
    from app.core.database import AsyncSessionLocal

    _ = force
    if _sample_lock.locked():
        return {"skipped": 1, "busy": 1}
    async with _sample_lock:
        t0 = time.monotonic()
        async with AsyncSessionLocal() as db:
            stats = await _sample_panels_with_db(db)
        stats["ms"] = int((time.monotonic() - t0) * 1000)
        return stats


def _coeff_from_panel_config(config: dict | None) -> float:
    """Local coeff reader — avoid importing heavy orchestrator on every overview."""
    from app.services.quota import norm_coeff

    try:
        return float(norm_coeff((config or {}).get("coefficient", 1)))
    except Exception:  # noqa: BLE001
        return 1.0


async def _astra_client_raw_by_panel(db: AsyncSession) -> dict[int, int]:
    """Σ (used_down+used_up) from AstraNodeClient — source of truth for Astra."""
    from app.models.astra_outbound import AstraNodeClient

    rows = (
        await db.execute(
            select(
                AstraNodeClient.panel_id,
                func.coalesce(
                    func.sum(
                        func.coalesce(AstraNodeClient.used_down, 0)
                        + func.coalesce(AstraNodeClient.used_up, 0)
                    ),
                    0,
                ),
            ).group_by(AstraNodeClient.panel_id)
        )
    ).all()
    return {int(pid): max(0, int(total or 0)) for pid, total in rows}


async def live_panel_raw_and_weighted(
    db: AsyncSession,
) -> dict[int, dict[str, int]]:
    """Σ raw and Σ (raw × group coefficient) per panel.

    For Astra panels, ``AstraNodeClient`` cumulatives win when they are ahead
    of ``PanelAccount`` (covers email-mismatch push gaps).
    """
    from app.models.group import GroupPanel
    from app.services.quota import norm_coeff, weighted_bytes

    gp_rows = (
        await db.execute(
            select(GroupPanel.panel_id, GroupPanel.group_id, GroupPanel.config)
        )
    ).all()
    claim: dict[tuple[int, int], float] = {}
    panel_max: dict[int, float] = {}
    for pid, gid, cfg in gp_rows:
        c = _coeff_from_panel_config(cfg if isinstance(cfg, dict) else {})
        claim[(int(pid), int(gid))] = c
        panel_max[int(pid)] = max(float(panel_max.get(int(pid), 0.0)), c)

    # Same statuses as User.used_traffic / dashboard (pending does not count)
    rows = (
        await db.execute(
            select(
                PanelAccount.panel_id,
                PanelAccount.group_id,
                PanelAccount.used_traffic,
                PanelAccount.applied_coefficient,
            ).where(
                PanelAccount.status.in_(
                    (
                        PanelAccountStatus.active,
                        PanelAccountStatus.error,
                        PanelAccountStatus.deleted,
                    )
                )
            )
        )
    ).all()

    out: dict[int, dict[str, int]] = {}
    for pid, gid, raw, applied in rows:
        pid_i = int(pid)
        raw_i = max(0, int(raw or 0))
        if applied is not None:
            coeff = norm_coeff(applied)
        elif gid is not None:
            coeff = claim.get((pid_i, int(gid)), panel_max.get(pid_i, 1.0) or 1.0)
        else:
            coeff = panel_max.get(pid_i, 1.0) or 1.0
        slot = out.setdefault(pid_i, {"raw": 0, "weighted": 0})
        slot["raw"] += raw_i
        slot["weighted"] += int(weighted_bytes(raw_i, coeff))

    # Astra clients are authoritative when ahead of mirrored PanelAccount rows
    try:
        client_raw = await _astra_client_raw_by_panel(db)
    except Exception:  # noqa: BLE001
        logger.debug("astra client raw overlay failed", exc_info=True)
        client_raw = {}
    for pid, craw in client_raw.items():
        slot = out.setdefault(int(pid), {"raw": 0, "weighted": 0})
        acc_raw = int(slot["raw"] or 0)
        if craw <= acc_raw:
            continue
        if acc_raw > 0:
            ratio = craw / acc_raw
            slot["weighted"] = int(round(int(slot["weighted"] or 0) * ratio))
        else:
            coeff = panel_max.get(int(pid), 1.0) or 1.0
            slot["weighted"] = int(weighted_bytes(craw, coeff))
        slot["raw"] = craw
    return out


async def ensure_panel_cursors_seeded(
    db: AsyncSession,
    live_totals: dict[int, int],
    *,
    now: datetime | None = None,
) -> int:
    """Seed panel cursors at current live WITHOUT dumping lifetime into hours.

    Must run *before* Xray sync so the post-sync growth is flushed into the
    current chart hour instead of being absorbed into a first-seed baseline.
    """
    now = _as_utc(now) or _utc_now()
    n = 0
    for pid, live in live_totals.items():
        try:
            async with db.begin_nested():
                state = await _get_or_create_panel_state(db, int(pid))
                if state.last_sample_at is not None:
                    continue
                state.last_raw_total = max(0, int(live or 0))
                state.last_sample_at = now
                n += 1
        except Exception:  # noqa: BLE001
            logger.debug("seed panel cursor p=%s failed", pid, exc_info=True)
    if n:
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            logger.debug("seed panel cursors commit failed", exc_info=True)
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
    return n


async def dashboard_weighted_total(db: AsyncSession) -> int:
    """Σ User.used_traffic — same number as dashboard «ترافیک مصرفی»."""
    from app.models.user import User

    return int(
        (
            await db.execute(
                select(func.coalesce(func.sum(User.used_traffic), 0))
            )
        ).scalar()
        or 0
    )


async def live_panel_totals(db: AsyncSession) -> dict[int, int]:
    """Current Σ PanelAccount.used_traffic per panel (authoritative cumulative)."""
    usage = await live_panel_raw_and_weighted(db)
    return {pid: int(v["raw"]) for pid, v in usage.items()}


async def sync_astra_panels_live(
    db: AsyncSession,
    *,
    timeout_per_panel: float = 2.0,
    rebuild_on_limit: bool = False,
) -> int:
    """Pull fresh Xray traffic into PanelAccount for all active Astra panels.

    Analytics path defaults ``rebuild_on_limit=False`` so quota flips never
    stall the stats page behind an xray/nginx rebuild.
    """
    from app.models.panel import PanelType
    from app.services import astra_node as astra_node_svc

    panels = list(
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
    if not panels:
        return 0

    updated = 0
    for panel in panels:
        try:
            updated += int(
                await astra_node_svc.sync_traffic_from_xray(
                    db,
                    panel,
                    timeout=timeout_per_panel,
                    rebuild_on_limit=rebuild_on_limit,
                )
                or 0
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "astra analytics sync panel=%s failed", panel.id, exc_info=True
            )
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.debug("astra analytics sync commit failed", exc_info=True)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
    return updated


async def flush_live_deltas_to_hours(
    db: AsyncSession,
    live_totals: dict[int, int],
    *,
    now: datetime | None = None,
) -> int:
    """Write unflushed live growth into the *current* hour bucket.

    Unlike the background sampler, catch-up after Xray sync is credited entirely
    to the current hour — spreading a multi-hour idle gap would invent fake
    traffic in empty past hours and under-count the active hour.
    """
    now = _as_utc(now) or _utc_now()
    hour_ts = _hour_ts(now)
    touched = 0
    for pid, live in live_totals.items():
        try:
            async with db.begin_nested():
                state = await _get_or_create_panel_state(db, int(pid))
                raw = max(0, int(live))
                prev = int(state.last_raw_total or 0)
                last_at = _as_utc(state.last_sample_at)
                if last_at is None:
                    # First seed: adopt baseline, do not dump lifetime into chart
                    state.last_raw_total = raw
                    state.last_sample_at = now
                    touched += 1
                    continue
                delta = 0 if raw < prev else (raw - prev)
                if delta > 0:
                    await _bump_panel_hour(
                        db,
                        int(pid),
                        hour_ts,
                        delta,
                        active_seconds=0,
                        peak_bps=0.0,
                        online_n=0,
                    )
                state.last_raw_total = raw
                state.last_sample_at = now
                touched += 1
        except Exception:  # noqa: BLE001
            logger.debug("flush live hour p=%s failed", pid, exc_info=True)
    if touched:
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            logger.debug("flush live hour commit failed", exc_info=True)
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
    return touched


def _scale_weighted(raw_part: int, raw_total: int, weighted_total: int) -> int:
    """Map a raw period slice to weighted using the panel's live raw→weighted ratio."""
    r = max(0, int(raw_part or 0))
    rt = max(0, int(raw_total or 0))
    wt = max(0, int(weighted_total or 0))
    if r <= 0 or rt <= 0:
        return 0
    return int(round(r * (wt / rt)))


def _period_bytes_from_panel(
    *,
    live: int,
    pending: int,
    bucket: int,
    watermark: int,
) -> int:
    """Hour/day from our sampled buckets (+safe pending), floored by live watermark.

    Pending larger than a short sampling lag is ignored so a stale cursor cannot
    dump lifetime bytes into «امروز» / «این ساعت».
    """
    live_i = max(0, int(live or 0))
    if live_i <= 0:
        return 0
    bucket_i = max(0, int(bucket or 0))
    pending_i = max(0, int(pending or 0))
    # ~1–few minutes of lag, or 5% of live — never a multi-day backlog
    pending_cap = max(
        64 * 1024 * 1024,
        bucket_i * 3,
        int(live_i * 0.05),
    )
    safe_pending = min(pending_i, pending_cap)
    from_buckets = min(bucket_i + safe_pending, live_i)
    from_wm = min(max(0, int(watermark or 0)), live_i)
    return max(from_buckets, from_wm)


async def refresh_panels_live(
    *,
    pull_remotes: bool = False,
    astra_only: bool = True,
) -> dict[str, Any]:
    """Refresh panel analytics sources.

    Default is Astra-only (local Xray statsquery) — fast and accurate for the
    panel stats page. Remote Marzban/etc pulls are opt-in and can take minutes.
    """
    from app.core.database import AsyncSessionLocal
    from app.models.user import User
    from app.services.orchestrator import refresh_users_usage_batch

    t0 = time.monotonic()
    pulled = 0
    astra_synced = 0

    async with AsyncSessionLocal() as db:
        # 1) Fast path: Astra node local stats (always)
        try:
            astra_synced = await sync_astra_panels_live(
                db, timeout_per_panel=1.2, rebuild_on_limit=False
            )
        except Exception:  # noqa: BLE001
            logger.warning("astra bulk sync for analytics failed", exc_info=True)

        # 2) Optional remote pull (slow) — disabled by default
        if pull_remotes and not astra_only:
            try:
                users = list(
                    (
                        await db.execute(
                            select(User)
                            .join(PanelAccount, PanelAccount.user_id == User.id)
                            .distinct()
                            .order_by(User.id.desc())
                            .limit(80)
                        )
                    )
                    .scalars()
                    .unique()
                    .all()
                )
                if users:
                    await asyncio.wait_for(
                        refresh_users_usage_batch(
                            db,
                            users,
                            force=True,
                            concurrency=8,
                            push_remotes=False,
                            quick=True,
                            isolated=False,
                        ),
                        timeout=12.0,
                    )
                    await db.commit()
                    pulled = len(users)
            except Exception:  # noqa: BLE001
                logger.warning("panel analytics remote pull failed", exc_info=True)
                try:
                    await db.rollback()
                except Exception:  # noqa: BLE001
                    pass

    sample_stats = await sample_all_panels_once(force=True)
    return {
        "ok": True,
        "users_pulled": pulled,
        "astra_synced": astra_synced,
        "astra_only": bool(astra_only or not pull_remotes),
        **sample_stats,
        "ms": int((time.monotonic() - t0) * 1000),
    }


async def refresh_panel_analytics_fast(
    *,
    tz_offset_min: int = 0,
    budget_s: float = 18.0,
) -> dict[str, Any]:
    """One-shot fast refresh for the admin «تازه‌سازی زنده» button.

    Seed cursors → Xray sync (enough time to finish) → flush chart hour →
    overview. Never pulls remote panels or samples every user.
    """
    from app.core.database import AsyncSessionLocal

    t0 = time.monotonic()
    deadline = t0 + max(8.0, float(budget_s))
    astra_synced = 0
    bytes_hint = 0
    overview: dict[str, Any] | None = None
    err: str | None = None

    async with AsyncSessionLocal() as db:
        # 1) Snapshot live + seed cursors BEFORE sync so growth is flushable
        try:
            pre = await live_panel_raw_and_weighted(db)
            pre_live = {pid: int(v["raw"]) for pid, v in pre.items()}
            await ensure_panel_cursors_seeded(db, pre_live, now=_utc_now())
        except Exception:  # noqa: BLE001
            logger.debug("fast refresh pre-seed failed", exc_info=True)
            pre_live = {}

        remain = deadline - time.monotonic()
        if remain > 1.0:
            try:
                astra_synced = int(
                    await asyncio.wait_for(
                        sync_astra_panels_live(
                            db,
                            timeout_per_panel=min(5.0, max(2.0, remain / 2)),
                            rebuild_on_limit=False,
                        ),
                        timeout=min(10.0, remain - 0.5),
                    )
                    or 0
                )
            except Exception as exc:  # noqa: BLE001
                err = f"sync:{type(exc).__name__}"
                logger.debug("fast refresh astra sync failed", exc_info=True)

        try:
            post = await live_panel_raw_and_weighted(db)
            post_live = {pid: int(v["raw"]) for pid, v in post.items()}
            bytes_hint = sum(
                max(0, int(post_live.get(pid, 0)) - int(pre_live.get(pid, 0)))
                for pid in set(pre_live) | set(post_live)
            )
            await flush_live_deltas_to_hours(db, post_live, now=_utc_now())
        except Exception:  # noqa: BLE001
            logger.debug("fast refresh flush failed", exc_info=True)

        try:
            await sample_all_panels_once(force=False)
        except Exception:  # noqa: BLE001
            logger.debug("fast refresh panel sample failed", exc_info=True)

        remain = deadline - time.monotonic()
        try:
            overview = await asyncio.wait_for(
                build_panel_overview(
                    db,
                    tz_offset_min=tz_offset_min,
                    sync_astra=False,
                    persist=True,
                ),
                timeout=max(2.0, remain),
            )
        except Exception as exc:  # noqa: BLE001
            err = f"overview:{type(exc).__name__}"
            logger.exception("fast refresh overview failed")
            raise

    return {
        "ok": True,
        "stats": {
            "astra_synced": astra_synced,
            "astra_only": True,
            "users_pulled": 0,
            "bytes_added": int(bytes_hint),
            "ms": int((time.monotonic() - t0) * 1000),
            "error": err,
        },
        "overview": overview,
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
    if int(end_ts) <= int(start_ts):
        return 0
    lo = _align_down_hour(int(start_ts))
    hi = _align_up_hour(int(end_ts))
    rows = (
        await db.execute(
            select(PanelUsageHourBucket.hour_ts, PanelUsageHourBucket.raw_bytes).where(
                PanelUsageHourBucket.panel_id == int(panel_id),
                PanelUsageHourBucket.hour_ts >= lo,
                PanelUsageHourBucket.hour_ts < hi,
            )
        )
    ).all()
    return sum(
        _overlap_bytes(int(ts), int(raw or 0), int(start_ts), int(end_ts))
        for ts, raw in rows
    )


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
    """Online users by panel from Astra traffic cursors (not remote panel APIs).

    Prefer per-panel ``PanelUserTrafficState.online_since``. Users only marked
    online globally are attributed to the panel where they have the most raw
    traffic. If nothing is marked online, ``available`` is false so UI can hide it.
    """
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

    global_online = {
        int(s.user_id)
        for s in (
            await db.execute(
                select(UserUsageState).where(UserUsageState.online_since.is_not(None))
            )
        )
        .scalars()
        .all()
    }
    missing = global_online - online_uids
    if missing:
        acc_rows = (
            await db.execute(
                select(
                    PanelAccount.user_id,
                    PanelAccount.panel_id,
                    PanelAccount.used_traffic,
                ).where(
                    PanelAccount.user_id.in_(list(missing)),
                    PanelAccount.status.in_(
                        (
                            PanelAccountStatus.active,
                            PanelAccountStatus.error,
                            PanelAccountStatus.pending,
                        )
                    ),
                )
            )
        ).all()
        best: dict[int, tuple[int, int]] = {}
        for uid, pid, raw in acc_rows:
            uid_i, pid_i, raw_i = int(uid), int(pid), int(raw or 0)
            prev = best.get(uid_i)
            if prev is None or raw_i > prev[1]:
                best[uid_i] = (pid_i, raw_i)
        for uid_i, (pid_i, _) in best.items():
            by_panel.setdefault(pid_i, set()).add(uid_i)
            online_uids.add(uid_i)

    all_online = online_uids | global_online
    panels_out = [
        {
            "panel_id": pid,
            "online": len(uids),
            "operators": {},
            "brands": {},
        }
        for pid, uids in sorted(by_panel.items(), key=lambda kv: -len(kv[1]))
    ]
    total = len(all_online)
    panel_slots = sum(len(v) for v in by_panel.values())
    return {
        "available": total > 0,
        "source": "astra_traffic",
        "total_online": total,
        "panel_online_total": panel_slots,
        "by_panel": panels_out,
        "operators": {},
        "brands": {},
    }


def _chart_hour_bytes(*, live: int, bucket: int, pending: int) -> int:
    """Bytes for the current chart column (e.g. 19:30→now).

    After a flush, ``pending`` is ~0 and ``bucket`` holds the column total.
    Before flush, ``bucket + pending`` recovers unsampled live growth without
    the aggressive caps used elsewhere for stale-cursor safety.
    """
    live_i = max(0, int(live or 0))
    if live_i <= 0:
        return 0
    return min(live_i, max(0, int(bucket or 0)) + max(0, int(pending or 0)))


async def build_panel_overview(
    db: AsyncSession,
    *,
    tz_offset_min: int = 0,
    sync_astra: bool = True,
    persist: bool = True,
) -> dict[str, Any]:
    """Panel analytics overview for the admin UI.

    Flow for accurate «این ساعت» (= chart column start → now):
      1) seed cursors at pre-sync live (no lifetime dump)
      2) Astra/Xray sync into clients + PanelAccount
      3) flush live−cursor into the current chart-hour bucket
      4) read that bucket as the hour total

    Day uses local midnight buckets. ``persist=False`` skips the flush
    (debug only) — default is always flush so the current column stays live.
    """
    now = _utc_now()
    periods = _period_starts(now, tz_offset_min)
    day = int(periods["day"])
    day_end = int(periods["day_end"])
    # Same bucket as the chart's current column (UTC-aligned hour start).
    grid_hour = int(periods["grid_hour"])
    chart_hour = grid_hour
    chart_hour_end = grid_hour + 3600
    tz_off = int(periods["tz_offset_min"])
    g_start = grid_hour - 23 * 3600

    panels = list(
        (await db.execute(select(Panel).order_by(Panel.id.asc()))).scalars().all()
    )

    # Pre-sync live → seed cursors so Xray growth is flushable into this hour
    try:
        pre_map = await live_panel_raw_and_weighted(db)
    except Exception:  # noqa: BLE001
        logger.exception("pre-sync live_panel_raw_and_weighted failed")
        pre_map = {}
    pre_live = {pid: int(v["raw"]) for pid, v in pre_map.items()}
    for p in panels:
        pre_live.setdefault(int(p.id), 0)
    try:
        await ensure_panel_cursors_seeded(db, pre_live, now=now)
    except Exception:  # noqa: BLE001
        logger.debug("ensure panel cursors seeded failed", exc_info=True)

    if sync_astra:
        try:
            await asyncio.wait_for(
                sync_astra_panels_live(
                    db, timeout_per_panel=4.0, rebuild_on_limit=False
                ),
                timeout=9.0,
            )
        except Exception:  # noqa: BLE001
            logger.debug("astra sync before panel overview failed", exc_info=True)

    try:
        usage_map = await live_panel_raw_and_weighted(db)
    except Exception:  # noqa: BLE001
        logger.exception("live_panel_raw_and_weighted failed")
        usage_map = {}
    live_totals = {pid: int(v["raw"]) for pid, v in usage_map.items()}
    weighted_totals = {pid: int(v["weighted"]) for pid, v in usage_map.items()}
    for p in panels:
        live_totals.setdefault(int(p.id), 0)
        weighted_totals.setdefault(int(p.id), 0)

    # Credit post-sync growth into the current chart column
    if persist:
        try:
            await flush_live_deltas_to_hours(db, live_totals, now=now)
        except Exception:  # noqa: BLE001
            logger.debug("flush live deltas before overview failed", exc_info=True)

    states = {
        int(s.panel_id): s
        for s in (await db.execute(select(PanelUsageState))).scalars().all()
    }

    accounts_by_panel: dict[int, int] = {
        int(pid): int(cnt or 0)
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
        ).all()
    }

    # Buckets: 24h chart + current chart-hour / local day
    bucket_rows = (
        await db.execute(
            select(
                PanelUsageHourBucket.panel_id,
                PanelUsageHourBucket.hour_ts,
                PanelUsageHourBucket.raw_bytes,
            ).where(
                PanelUsageHourBucket.hour_ts >= min(g_start, day) - 3600,
                PanelUsageHourBucket.hour_ts <= grid_hour,
            )
        )
    ).all()
    day_map: dict[int, int] = {}
    hour_map: dict[int, int] = {}
    raw_by_ts: dict[int, int] = {g_start + i * 3600: 0 for i in range(24)}
    w_by_ts: dict[int, int] = {g_start + i * 3600: 0 for i in range(24)}
    for pid, ts, raw in bucket_rows:
        pid_i, ts_i, raw_i = int(pid), int(ts), max(0, int(raw or 0))
        live_r = int(live_totals.get(pid_i, 0))
        live_w = int(weighted_totals.get(pid_i, 0))
        # Exact chart column (grid hour), e.g. 19:30→20:30 local in IR
        if ts_i == chart_hour:
            hour_map[pid_i] = hour_map.get(pid_i, 0) + raw_i
        od = _overlap_bytes(ts_i, raw_i, day, day_end)
        if od:
            day_map[pid_i] = day_map.get(pid_i, 0) + od
        if ts_i in raw_by_ts:
            raw_by_ts[ts_i] += raw_i
            w_by_ts[ts_i] += _scale_weighted(raw_i, live_r, live_w)

    rows: list[dict[str, Any]] = []
    total_live = 0
    total_weighted = 0
    total_pending = 0
    total_hour = 0
    total_day = 0
    total_hour_w = 0
    total_day_w = 0
    marks_touched = False
    for p in panels:
        pid = int(p.id)
        st = states.get(pid)
        if st is None:
            st = await _get_or_create_panel_state(db, pid)
            states[pid] = st
            marks_touched = True
        live = int(live_totals.get(pid, 0))
        live_w = int(weighted_totals.get(pid, 0))
        cursor = int(st.last_raw_total or 0)
        seeded = bool(st.last_sample_at is not None)
        pending = _pending_bytes(live=live, cursor=cursor, seeded=seeded)
        bucket_h = int(hour_map.get(pid, 0))
        bucket_d = int(day_map.get(pid, 0))
        # Authoritative: flushed chart-hour bucket (+ residual pending)
        h = _chart_hour_bytes(live=live, bucket=bucket_h, pending=pending)
        d = _chart_hour_bytes(live=live, bucket=bucket_d, pending=pending)
        if d < h:
            d = h
        # Keep watermarks aligned to the chart column so later live growth
        # between flushes stays consistent (never seed mid-hour to full live).
        st.period_hour_ts = chart_hour
        st.period_hour_raw = max(0, live - h)
        st.period_day_ts = day
        st.period_day_raw = max(0, live - d)
        marks_touched = True
        h_w = _scale_weighted(h, live, live_w)
        d_w = _scale_weighted(d, live, live_w)
        total_live += live
        total_weighted += live_w
        total_pending += pending
        total_hour += h
        total_day += d
        total_hour_w += h_w
        total_day_w += d_w
        rows.append(
            {
                "panel_id": pid,
                "name": p.name,
                "type": getattr(p.type, "value", p.type),
                "is_active": bool(getattr(p, "is_active", True)),
                "accounts": accounts_by_panel.get(pid, 0),
                "online": 0,
                "raw": {
                    "current_total": live,
                    "pending": pending,
                    "hour": h,
                    "day": d,
                    "week": 0,
                    "month": 0,
                    "lifetime_cursor": cursor,
                },
                "weighted": {
                    "current_total": live_w,
                    "hour": h_w,
                    "day": d_w,
                },
                "compare_hour_pct": None,
                "tests": {
                    "runs": int(getattr(st, "tests_total", 0) or 0),
                    "configs_tested": 0,
                    "top100": 0,
                    "good": 0,
                    "all_ops_ok": 0,
                    "last_test_at": None,
                    "last_run": None,
                },
            }
        )

    if marks_touched:
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            logger.debug("panel period mark commit failed", exc_info=True)
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass

    # Chart current bar must equal «این ساعت» totals
    raw_by_ts[grid_hour] = int(total_hour)
    w_by_ts[grid_hour] = int(total_hour_w)

    rows.sort(
        key=lambda r: (-int(r["raw"]["current_total"] or 0), r["name"])
    )

    def _top_row(key: str) -> dict[str, Any] | None:
        if not rows:
            return None
        best = max(rows, key=lambda r: int(r["raw"].get(key) or 0))
        val = int(best["raw"].get(key) or 0)
        if val <= 0:
            return None
        return {
            "panel_id": best["panel_id"],
            "name": best["name"],
            "raw_bytes": val,
            "metric": key,
        }

    global_hourly = []
    for i in range(24):
        ts = g_start + i * 3600
        raw_i = int(raw_by_ts.get(ts, 0))
        w_i = int(w_by_ts.get(ts, 0))
        if w_i < raw_i:
            w_i = raw_i
        global_hourly.append(
            {
                "hour_ts": ts,
                "raw_bytes": raw_i,
                "weighted_bytes": w_i,
                "is_current": ts == grid_hour,
            }
        )

    by_hod = [0] * 24
    for ts, raw in raw_by_ts.items():
        by_hod[_hod_index(int(ts), tz_off)] += int(raw or 0)
    peak_hod = max(range(24), key=lambda h: by_hod[h]) if any(by_hod) else None

    try:
        dash_weighted = await dashboard_weighted_total(db)
    except Exception:  # noqa: BLE001
        logger.exception("dashboard weighted total failed")
        dash_weighted = total_weighted

    top_day_list = [
        {
            "panel_id": r["panel_id"],
            "name": r["name"],
            "raw_bytes": int(r["raw"]["current_total"] or 0),
            "current_total": int(r["raw"]["current_total"] or 0),
            "online": 0,
        }
        for r in rows
        if int(r["raw"]["current_total"] or 0) > 0
    ][:12]

    return {
        "generated_at": now.isoformat(),
        "tz_offset_min": tz_off,
        "show_online": False,
        "online": {
            "available": False,
            "source": "none",
            "total_online": 0,
            "panel_online_total": 0,
            "by_panel": [],
            "operators": {},
            "brands": {},
        },
        "totals": {
            "raw_current": total_live,
            "weighted_current": int(dash_weighted),
            "account_weighted": total_weighted,
            "raw_pending": total_pending,
            "raw_hour": total_hour,
            "weighted_hour": total_hour_w,
            "raw_day": total_day,
            "weighted_day": total_day_w,
            "panels": len(panels),
        },
        "top": {
            "by_current": _top_row("current_total"),
            "by_hour": _top_row("hour"),
            "by_day": _top_row("day"),
        },
        "top_day_panels": top_day_list,
        "global_hourly": global_hourly,
        "hour_of_day": {
            "bytes": by_hod,
            "peak_hour": peak_hod,
            "peak_bytes": by_hod[peak_hod] if peak_hod is not None else 0,
        },
        "panels": rows,
        "chart_hour_ts": chart_hour,
        "chart_hour_end_ts": chart_hour_end,
        "note": (
            "بدون ضریب = Σ خام اکانت‌های Astra. با ضریب = ترافیک مصرفی داشبورد. "
            "«این ساعت» = باکت همان ستون نمودار (مثلاً ۱۹:۳۰) بعد از همگام‌سازی Xray."
        ),
    }


async def build_panel_detail(
    db: AsyncSession,
    panel_id: int,
    *,
    tz_offset_min: int = 0,
) -> dict[str, Any]:
    panel = await db.get(Panel, int(panel_id))
    if panel is None:
        raise KeyError("panel")
    now = _utc_now()
    periods = _period_starts(now, tz_offset_min)
    day = int(periods["day"])
    day_end = int(periods["day_end"])
    week = int(periods["week"])
    week_end = int(periods["week_end"])
    month = int(periods["month"])
    next_month_ts = int(periods["next_month"])
    prev_month = int(periods["prev_month"])
    grid_hour = int(periods["grid_hour"])
    # Match overview/chart: current column start → now
    hour = grid_hour
    hour_end = grid_hour + 3600
    tz_off = int(periods["tz_offset_min"])

    st = await _get_or_create_panel_state(db, int(panel_id))
    try:
        from app.models.panel import PanelType

        if panel.type == PanelType.astra:
            from app.services import astra_node as astra_node_svc

            await astra_node_svc.sync_traffic_from_xray(
                db, panel, timeout=1.5, rebuild_on_limit=False
            )
            await db.commit()
            st = await _get_or_create_panel_state(db, int(panel_id))
    except Exception:  # noqa: BLE001
        logger.debug("astra sync before panel detail failed", exc_info=True)
    try:
        usage_map = await live_panel_raw_and_weighted(db)
    except Exception:  # noqa: BLE001
        logger.exception("live usage for panel detail failed")
        usage_map = {}
    live_total = int(usage_map.get(int(panel_id), {}).get("raw", 0))
    live_weighted = int(usage_map.get(int(panel_id), {}).get("weighted", 0))
    try:
        await flush_live_deltas_to_hours(
            db, {int(panel_id): live_total}, now=now
        )
        st = await _get_or_create_panel_state(db, int(panel_id))
    except Exception:  # noqa: BLE001
        logger.debug("flush before panel detail failed", exc_info=True)
    cursor = int(st.last_raw_total or 0)
    seeded = bool(st.last_sample_at is not None)
    pending = _pending_bytes(live=live_total, cursor=cursor, seeded=seeded)
    bucket_h = await _sum_panel_hours(db, panel_id, hour, hour_end)
    bucket_d = await _sum_panel_hours(db, panel_id, day, day_end)
    cur_h = _chart_hour_bytes(
        live=live_total, bucket=bucket_h, pending=pending
    )
    cur_d = _chart_hour_bytes(
        live=live_total, bucket=bucket_d, pending=pending
    )
    if cur_d < cur_h:
        cur_d = cur_h
    st.period_hour_ts = hour
    st.period_hour_raw = max(0, live_total - cur_h)
    st.period_day_ts = day
    st.period_day_raw = max(0, live_total - cur_d)
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.debug("panel detail mark commit failed", exc_info=True)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
    cur_h_w = _scale_weighted(cur_h, live_total, live_weighted)
    cur_d_w = _scale_weighted(cur_d, live_total, live_weighted)
    prev_h = min(await _sum_panel_hours(db, panel_id, hour - 3600, hour), live_total)
    prev_d = min(await _sum_panel_hours(db, panel_id, day - 86400, day), live_total)
    cur_w = min(
        (await _sum_panel_hours(db, panel_id, week, week_end)) + pending, live_total
    )
    prev_w = min(
        await _sum_panel_hours(db, panel_id, week - 7 * 86400, week), live_total
    )
    cur_m = min(
        (await _sum_panel_hours(db, panel_id, month, next_month_ts)) + pending,
        live_total,
    )
    prev_m = min(await _sum_panel_hours(db, panel_id, prev_month, month), live_total)

    series_start = grid_hour - 47 * 3600
    buckets = await _hour_rows_panel(db, panel_id, series_start, grid_hour + 3600)
    by_h = {int(b.hour_ts): b for b in buckets}
    hourly = []
    for i in range(48):
        ts = series_start + i * 3600
        b = by_h.get(ts)
        raw = int(b.raw_bytes or 0) if b else 0
        if i == 47:
            raw += pending
        sc = int(b.sample_count or 0) if b else 0
        on_sum = int(b.online_sum or 0) if b else 0
        avg_online = (on_sum / sc) if sc else 0.0
        hourly.append(
            {
                "hour_ts": ts,
                "raw_bytes": raw,
                "avg_online": round(avg_online, 2),
                "online_peak": int(b.online_peak or 0) if b else 0,
            }
        )

    heat_start = grid_hour - 14 * 86400
    heat_rows = await _hour_rows_panel(db, panel_id, heat_start, grid_hour + 3600)
    by_hod = [0] * 24
    for b in heat_rows:
        by_hod[_hod_index(int(b.hour_ts), tz_off)] += int(b.raw_bytes or 0)
    if pending:
        by_hod[_hod_index(grid_hour, tz_off)] += pending
    peak_hod = max(range(24), key=lambda h: by_hod[h]) if any(by_hod) else None

    online_info = await _online_breakdown(db)
    show_online = bool(online_info.get("available"))
    panel_online = next(
        (x for x in online_info["by_panel"] if int(x["panel_id"]) == int(panel_id)),
        {"online": 0, "operators": {}, "brands": {}},
    )
    if not show_online:
        panel_online = {"online": 0, "operators": {}, "brands": {}}

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
        "show_online": show_online,
        "online": panel_online,
        "tz_offset_min": tz_off,
        "lifetime_raw": cursor,
        "current_total": live_total,
        "current_weighted": live_weighted,
        "pending_raw": pending,
        "weighted": {
            "current_total": live_weighted,
            "hour": cur_h_w,
            "day": cur_d_w,
        },
        "compare": {
            "hour": {
                "current": cur_h,
                "previous": prev_h,
                "change_pct": _pct_change(cur_h, prev_h),
                "weighted": cur_h_w,
            },
            "day": {
                "current": cur_d,
                "previous": prev_d,
                "change_pct": _pct_change(cur_d, prev_d),
                "weighted": cur_d_w,
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
            "last_run": (
                {
                    "tested": int(tests[0].tested or 0),
                    "in_top100": int(tests[0].in_top100 or 0),
                    "good": int(tests[0].good or 0),
                    "all_ops_ok": int(tests[0].all_ops_ok or 0),
                    "avg_ping": float(tests[0].avg_ping or -1),
                    "ts": tests[0].ts.isoformat() if tests[0].ts else None,
                }
                if tests
                else None
            ),
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
    }
