"""Collect and query per-user analytics (raw usage, sessions, sub clients)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import PanelAccount, PanelAccountStatus, User, UserStatus
from app.models.user_analytics import (
    SubClientProfile,
    SubFetchEvent,
    UsageHourBucket,
    UsageSession,
    UserUsageState,
)

logger = logging.getLogger("astra.analytics")

# Raw bytes in one sample window to count as "online"
_ONLINE_MIN_BYTES = 48 * 1024
# End session after this many consecutive low samples
_IDLE_STREAK_END = 2
# Keep detailed fetch events this long
_FETCH_RETENTION_DAYS = 45
_HOUR_RETENTION_DAYS = 120
_SESSION_RETENTION_DAYS = 120

# In-process debounce for sub-fetch logging
_sub_log_last: dict[tuple[int, str], float] = {}
_SUB_LOG_DEBOUNCE = 180.0  # seconds per (user, device_key)
_sample_lock = asyncio.Lock()

_CLIENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("v2rayNG", re.compile(r"v2rayng", re.I)),
    ("v2rayN", re.compile(r"v2rayn(?!g)", re.I)),
    ("Streisand", re.compile(r"streisand", re.I)),
    ("Hiddify", re.compile(r"hiddify", re.I)),
    ("HiddifyNext", re.compile(r"hiddifynext|hiddify.?next", re.I)),
    ("ClashMeta", re.compile(r"clash.?meta|mihomo", re.I)),
    ("Clash", re.compile(r"clash", re.I)),
    ("Shadowrocket", re.compile(r"shadowrocket", re.I)),
    ("Quantumult", re.compile(r"quantumult", re.I)),
    ("Surge", re.compile(r"surge", re.I)),
    ("NekoBox", re.compile(r"nekobox|nekoray", re.I)),
    ("v2box", re.compile(r"v2box", re.I)),
    ("FairVPN", re.compile(r"fairvpn", re.I)),
    ("FoXray", re.compile(r"foxray", re.I)),
    ("Stash", re.compile(r"stash", re.I)),
    ("SingBox", re.compile(r"sing-?box|sfa|sfm", re.I)),
    ("OkHttp", re.compile(r"okhttp", re.I)),
    ("AstraPing", re.compile(r"astraping", re.I)),
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


def _clamp_tz_offset(tz_offset_min: int | None) -> int:
    """JS Date.getTimezoneOffset() minutes; clamp to ±14h."""
    try:
        off = int(tz_offset_min or 0)
    except (TypeError, ValueError):
        off = 0
    return max(-14 * 60, min(14 * 60, off))


def _local_now(now: datetime, tz_offset_min: int) -> datetime:
    """Wall-clock local time for the client (tz-naive-ish UTC stamp shifted)."""
    return now - timedelta(minutes=int(tz_offset_min))


def _align_down_hour(ts: int) -> int:
    return int(ts) - (int(ts) % 3600)


def _align_up_hour(ts: int) -> int:
    t = int(ts)
    r = t % 3600
    return t if r == 0 else t + (3600 - r)


def _period_starts(now: datetime, tz_offset_min: int = 0) -> dict[str, int]:
    """Exact local period bounds as UTC unix seconds.

    Hour buckets are UTC-aligned; period sums prorate overlap (see
    ``_sum_hours_overlap``) so +30min zones stay accurate.
    """
    off = _clamp_tz_offset(tz_offset_min)
    local = _local_now(now, off)
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
    next_m = to_utc_ts(next_month)
    prev_m = to_utc_ts(prev_month)
    # Chart grid still uses UTC hour floors near "now"
    grid_hour = _align_down_hour(int(now.timestamp()))
    return {
        "hour": hour,
        "hour_end": hour + 3600,
        "day": day,
        "day_end": day + 86400,
        "week": week,
        "week_end": week + 7 * 86400,
        "month": month,
        "next_month": next_m,
        "prev_month": prev_m,
        "grid_hour": grid_hour,
        "tz_offset_min": off,
    }


def _hod_index(hour_ts: int, tz_offset_min: int = 0) -> int:
    """Map a UTC hour bucket to the client's local clock hour 0–23."""
    off = _clamp_tz_offset(tz_offset_min)
    local_ts = int(hour_ts) - off * 60
    return int((local_ts % 86400) // 3600)


def _overlap_bytes(hour_ts: int, raw: int, start_ts: int, end_ts: int) -> int:
    """Prorate a UTC hour bucket into [start_ts, end_ts)."""
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


def parse_client_app(user_agent: str) -> str:
    ua = (user_agent or "").strip()
    if not ua:
        return "نامشخص"
    for name, pat in _CLIENT_PATTERNS:
        if pat.search(ua):
            return name
    # Generic mobile / desktop hints
    low = ua.lower()
    if "android" in low:
        return "Android"
    if "iphone" in low or "ios" in low or "ipad" in low:
        return "iOS"
    if "windows" in low:
        return "Windows"
    if "mac os" in low or "macintosh" in low:
        return "macOS"
    if "linux" in low:
        return "Linux"
    return "سایر"


def _device_key(client_app: str, user_agent: str) -> str:
    # Same app + UA family ≈ same device class (multi-phone of same app still distinct UA)
    base = f"{client_app}|{(user_agent or '')[:180]}".lower()
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()[:20]


def _network_key(ip: str) -> str:
    ip = (ip or "").strip()
    if not ip or ip in ("127.0.0.1", "::1", "unknown"):
        return "local"
    # IPv4 /24
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
        parts = ip.split(".")
        return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
    # IPv6 — first 4 hextets
    if ":" in ip:
        hextets = ip.split(":")
        return ":".join(hextets[:4]) + "::/64"
    return ip


def extract_client_ip(headers: dict[str, str], fallback: str | None = None) -> str:
    def _h(name: str) -> str:
        for k, v in headers.items():
            if k.lower() == name.lower():
                return (v or "").strip()
        return ""

    for key in (
        "cf-connecting-ip",
        "true-client-ip",
        "x-real-ip",
    ):
        v = _h(key)
        if v:
            return v.split(",")[0].strip()
    xff = _h("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return (fallback or "").strip() or "unknown"


def format_bps(bps: float) -> str:
    n = float(bps or 0)
    if n <= 0:
        return "۰"
    # Prefer MB/s for display when large
    mbytes_s = n / 8.0 / (1024 * 1024)
    if mbytes_s >= 1:
        return f"{mbytes_s:.2f} MB/s"
    kbytes_s = n / 8.0 / 1024
    if kbytes_s >= 1:
        return f"{kbytes_s:.1f} KB/s"
    return f"{n / 8.0:.0f} B/s"


def _health_label(
    *,
    status: UserStatus | str,
    raw_24h: int,
    raw_1h: int,
    last_sub: datetime | None,
    last_traffic: datetime | None,
    device_count: int,
    network_count: int,
    online: bool,
    created_at: datetime | None,
) -> dict[str, str]:
    st = getattr(status, "value", status)
    now = _utc_now()
    if st in ("limited", "expired"):
        return {
            "code": "quota_done",
            "label": "حجم/زمان تمام",
            "tone": "warn",
            "hint": "کاربر به سقف رسیده؛ کانفیگ‌ها باید خاموش باشند",
        }
    if st == "disabled":
        return {
            "code": "disabled",
            "label": "خاموش",
            "tone": "muted",
            "hint": "توسط ادمین غیرفعال شده",
        }

    sub_age = (now - last_sub).total_seconds() if last_sub else None
    traffic_age = (now - last_traffic).total_seconds() if last_traffic else None
    age_account = (now - _as_utc(created_at)).total_seconds() if created_at else 0

    if online or raw_1h > 256 * 1024:
        if device_count >= 4 or network_count >= 5:
            return {
                "code": "multi_client",
                "label": "چنددستگاهی / پرترافیک",
                "tone": "warn",
                "hint": "چند کلاینت یا اینترنت مختلف روی یک ساب دیده شده",
            }
        return {
            "code": "healthy",
            "label": "وصل و سالم",
            "tone": "ok",
            "hint": "مصرف فعال و به‌نظر پایدار",
        }

    if sub_age is not None and sub_age < 6 * 3600 and raw_24h < 256 * 1024:
        return {
            "code": "struggling",
            "label": "اذیت می‌شود",
            "tone": "danger",
            "hint": "ساب را آپدیت کرده ولی مصرف نزدیک صفر است",
        }

    if (traffic_age is None or traffic_age > 3 * 86400) and (
        sub_age is None or sub_age > 3 * 86400
    ):
        if age_account < 2 * 86400 and raw_24h <= 0:
            return {
                "code": "never_used",
                "label": "هنوز استفاده نشده",
                "tone": "muted",
                "hint": "ساخته شده ولی مصرف/آپدیت ساب دیده نشده",
            }
        return {
            "code": "idle",
            "label": "بلااستفاده",
            "tone": "muted",
            "hint": "چند روز بدون مصرف و بدون آپدیت ساب",
        }

    if traffic_age is not None and traffic_age < 36 * 3600:
        return {
            "code": "ok_quiet",
            "label": "اخیراً فعال",
            "tone": "ok",
            "hint": "امروز/دیروز مصرف داشته، الان آرام است",
        }

    return {
        "code": "low",
        "label": "کم‌مصرف",
        "tone": "warn",
        "hint": "مصرف کم یا نامنظم نسبت به بازه‌های اخیر",
    }


async def _get_or_create_state(db: AsyncSession, user_id: int) -> UserUsageState:
    st = await db.get(UserUsageState, int(user_id))
    if st is None:
        st = UserUsageState(user_id=int(user_id), last_raw_total=0)
        db.add(st)
        await db.flush()
    return st


async def _bump_hour(
    db: AsyncSession,
    user_id: int,
    hour_ts: int,
    delta: int,
    active_seconds: int,
    peak_bps: float,
) -> None:
    if delta <= 0 and active_seconds <= 0:
        return
    row = (
        await db.execute(
            select(UsageHourBucket).where(
                UsageHourBucket.user_id == int(user_id),
                UsageHourBucket.hour_ts == int(hour_ts),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        db.add(
            UsageHourBucket(
                user_id=int(user_id),
                hour_ts=int(hour_ts),
                raw_bytes=max(0, int(delta)),
                active_seconds=max(0, int(active_seconds)),
                sample_count=1,
                peak_bps=max(0.0, float(peak_bps)),
            )
        )
    else:
        row.raw_bytes = int(row.raw_bytes or 0) + max(0, int(delta))
        row.active_seconds = int(row.active_seconds or 0) + max(0, int(active_seconds))
        row.sample_count = int(row.sample_count or 0) + 1
        if float(peak_bps) > float(row.peak_bps or 0):
            row.peak_bps = float(peak_bps)


async def _close_session(
    db: AsyncSession, state: UserUsageState, *, ended_at: datetime, raw_now: int
) -> None:
    if state.online_since is None:
        return
    started = _as_utc(state.online_since) or ended_at
    raw_start = int(state.online_raw_at_start or 0)
    used = max(0, int(raw_now) - raw_start)
    dur = max(1.0, (ended_at - started).total_seconds())
    avg = (used * 8.0) / dur if used > 0 else 0.0
    open_row = (
        await db.execute(
            select(UsageSession)
            .where(
                UsageSession.user_id == int(state.user_id),
                UsageSession.open == 1,
            )
            .order_by(UsageSession.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if open_row is not None:
        open_row.ended_at = ended_at
        open_row.raw_bytes = used
        open_row.avg_bps = avg
        if float(state.peak_bps or 0) > float(open_row.peak_bps or 0):
            # keep session peak as max seen during this window from samples
            pass
        open_row.open = 0
    else:
        db.add(
            UsageSession(
                user_id=int(state.user_id),
                started_at=started,
                ended_at=ended_at,
                raw_bytes=used,
                peak_bps=float(state.peak_bps or 0),
                avg_bps=avg,
                open=0,
            )
        )
    state.online_since = None
    state.online_raw_at_start = int(raw_now)
    state.idle_streak = 0


async def _open_session(
    db: AsyncSession, state: UserUsageState, *, started_at: datetime, raw_now: int
) -> None:
    state.online_since = started_at
    state.online_raw_at_start = int(raw_now)
    state.idle_streak = 0
    db.add(
        UsageSession(
            user_id=int(state.user_id),
            started_at=started_at,
            ended_at=None,
            raw_bytes=0,
            peak_bps=0.0,
            avg_bps=0.0,
            open=1,
        )
    )


async def _distribute_delta_hours(
    db: AsyncSession,
    user_id: int,
    delta: int,
    start: datetime,
    end: datetime,
    *,
    peak_bps: float = 0.0,
    active: bool = False,
) -> None:
    """Split ``delta`` across UTC hour buckets by elapsed-time proportion."""
    if delta <= 0:
        return
    start = _as_utc(start) or end
    end = _as_utc(end) or start
    if end <= start:
        await _bump_hour(db, user_id, _hour_ts(end), delta, 0, peak_bps)
        return
    total_sec = (end - start).total_seconds()
    # Cap absurd gaps — still attribute, but only across last 48h
    if total_sec > 48 * 3600:
        start = end - timedelta(hours=48)
        total_sec = (end - start).total_seconds()
    segs: list[tuple[int, float]] = []  # hour_ts, seconds
    t = start
    while t < end:
        hour_floor = _hour_ts(t)
        hour_end = datetime.fromtimestamp(hour_floor + 3600, tz=timezone.utc)
        seg_end = min(end, hour_end)
        segs.append((hour_floor, max(0.0, (seg_end - t).total_seconds())))
        t = seg_end
    if not segs:
        await _bump_hour(db, user_id, _hour_ts(end), delta, 0, peak_bps)
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
            await _bump_hour(db, user_id, h, part, act, peak_bps)


async def apply_raw_sample(
    db: AsyncSession,
    user_id: int,
    raw_total: int,
    *,
    now: datetime | None = None,
) -> None:
    """Ingest one raw-total sample for a user (idempotent per call)."""
    now = _as_utc(now) or _utc_now()
    state = await _get_or_create_state(db, user_id)
    prev = int(state.last_raw_total or 0)
    raw = max(0, int(raw_total))
    last_at = _as_utc(state.last_sample_at)
    # First ever sample: seed cursor only — never dump lifetime into one hour
    if last_at is None:
        state.last_raw_total = raw
        state.last_sample_at = now
        return

    dt = max(1.0, (now - last_at).total_seconds())
    # Rate estimate capped so a long idle gap doesn't invent insane peak
    dt_rate = max(1.0, min(600.0, dt))

    if raw < prev:
        # Remote/local reset — resync cursor, don't invent negative usage
        delta = 0
    else:
        delta = raw - prev

    rate_bps = (delta * 8.0) / dt_rate if delta > 0 else 0.0
    active = delta >= _ONLINE_MIN_BYTES
    await _distribute_delta_hours(
        db,
        user_id,
        delta,
        last_at,
        now,
        peak_bps=rate_bps,
        active=active,
    )

    if rate_bps > float(state.peak_bps or 0):
        state.peak_bps = float(rate_bps)
        state.peak_bps_at = now

    if state.online_since is not None:
        open_row = (
            await db.execute(
                select(UsageSession)
                .where(
                    UsageSession.user_id == int(user_id),
                    UsageSession.open == 1,
                )
                .order_by(UsageSession.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if open_row is not None and rate_bps > float(open_row.peak_bps or 0):
            open_row.peak_bps = float(rate_bps)
            open_row.raw_bytes = max(
                0, raw - int(state.online_raw_at_start or 0)
            )

    if delta >= _ONLINE_MIN_BYTES:
        if state.online_since is None:
            await _open_session(db, state, started_at=now, raw_now=raw)
        else:
            state.idle_streak = 0
    else:
        if state.online_since is not None:
            state.idle_streak = int(state.idle_streak or 0) + 1
            if state.idle_streak >= _IDLE_STREAK_END:
                await _close_session(db, state, ended_at=now, raw_now=raw)

    state.last_raw_total = raw
    state.last_sample_at = now


async def sample_all_users_once() -> dict[str, int]:
    """Read local raw counters and advance analytics (no remote I/O)."""
    from app.core.database import AsyncSessionLocal

    if _sample_lock.locked():
        return {"skipped": 1, "busy": 1}
    async with _sample_lock:
        t0 = time.monotonic()
        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(
                        PanelAccount.user_id,
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
                    .group_by(PanelAccount.user_id)
                )
            ).all()
            n = 0
            for uid, total in rows:
                try:
                    async with db.begin_nested():
                        await apply_raw_sample(db, int(uid), int(total or 0))
                    n += 1
                except Exception:  # noqa: BLE001
                    logger.exception("analytics sample user=%s failed", uid)
                if n % 40 == 0:
                    await db.commit()
            await db.commit()
        return {"users": n, "ms": int((time.monotonic() - t0) * 1000)}


async def prune_analytics(db: AsyncSession) -> dict[str, int]:
    now = _utc_now()
    cut_fetch = now - timedelta(days=_FETCH_RETENTION_DAYS)
    cut_hour = int((now - timedelta(days=_HOUR_RETENTION_DAYS)).timestamp())
    cut_sess = now - timedelta(days=_SESSION_RETENTION_DAYS)
    r1 = await db.execute(delete(SubFetchEvent).where(SubFetchEvent.ts < cut_fetch))
    r2 = await db.execute(delete(UsageHourBucket).where(UsageHourBucket.hour_ts < cut_hour))
    r3 = await db.execute(
        delete(UsageSession).where(
            UsageSession.open == 0,
            UsageSession.ended_at.is_not(None),
            UsageSession.ended_at < cut_sess,
        )
    )
    await db.commit()
    return {
        "fetch_events": int(r1.rowcount or 0),
        "hour_buckets": int(r2.rowcount or 0),
        "sessions": int(r3.rowcount or 0),
    }


async def record_sub_fetch(
    *,
    user_id: int,
    headers: dict[str, str],
    client_host: str | None = None,
    is_probe: bool = False,
) -> None:
    """Best-effort log of a subscription client update (own DB session)."""
    if is_probe:
        return
    from app.core.database import AsyncSessionLocal

    ua = ""
    for k, v in headers.items():
        if k.lower() == "user-agent":
            ua = (v or "")[:400]
            break
    if "astraping" in ua.lower():
        return

    ip = extract_client_ip(headers, client_host)
    app = parse_client_app(ua)
    dkey = _device_key(app, ua)
    nkey = _network_key(ip)
    from app.services.iran_operators import classify_operator
    from app.services.panel_analytics import parse_device_brand

    op_code = classify_operator(ip)
    brand = parse_device_brand(ua)
    uid = int(user_id)
    now_m = time.monotonic()
    debounce_key = (uid, dkey)
    last = _sub_log_last.get(debounce_key)
    if last is not None and (now_m - last) < _SUB_LOG_DEBOUNCE:
        return
    _sub_log_last[debounce_key] = now_m
    # Cap debounce map
    if len(_sub_log_last) > 8000:
        oldest = sorted(_sub_log_last.items(), key=lambda kv: kv[1])[:2000]
        for k, _ in oldest:
            _sub_log_last.pop(k, None)

    country = None
    for k, v in headers.items():
        if k.lower() == "cf-ipcountry" and v and v.upper() not in ("XX", "T1"):
            country = v.upper()[:8]
            break

    now = _utc_now()
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                SubFetchEvent(
                    user_id=uid,
                    ts=now,
                    client_ip=ip[:64],
                    network_key=nkey[:64],
                    device_key=dkey[:64],
                    client_app=app[:64],
                    user_agent=ua,
                    country=country,
                    operator=op_code[:16],
                    device_brand=brand[:32],
                )
            )
            await _upsert_profile(
                db,
                uid,
                kind="device",
                key=dkey,
                label=app,
                detail=(ua[:240] if ua else None),
                now=now,
            )
            await _upsert_profile(
                db,
                uid,
                kind="network",
                key=nkey,
                label=nkey if nkey != "local" else "لوکال",
                detail=ip,
                now=now,
            )
            await _upsert_profile(
                db,
                uid,
                kind="brand",
                key=brand.lower()[:64],
                label=brand,
                detail=ua[:240] if ua else None,
                now=now,
            )
            await _upsert_profile(
                db,
                uid,
                kind="operator",
                key=op_code[:64],
                label=op_code,
                detail=ip,
                now=now,
            )
            st = await _get_or_create_state(db, uid)
            st.last_sub_fetch_at = now
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.debug("sub fetch log failed user=%s", uid, exc_info=True)


async def _upsert_profile(
    db: AsyncSession,
    user_id: int,
    *,
    kind: str,
    key: str,
    label: str,
    detail: str | None,
    now: datetime,
) -> None:
    row = (
        await db.execute(
            select(SubClientProfile).where(
                SubClientProfile.user_id == int(user_id),
                SubClientProfile.kind == kind,
                SubClientProfile.key == key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        db.add(
            SubClientProfile(
                user_id=int(user_id),
                kind=kind,
                key=key,
                label=(label or "")[:200],
                detail=(detail[:500] if detail else None),
                hit_count=1,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
    else:
        row.hit_count = int(row.hit_count or 0) + 1
        row.last_seen_at = now
        if label:
            row.label = label[:200]
        if detail:
            row.detail = detail[:500]


async def _sum_hours(
    db: AsyncSession, user_id: int, start_ts: int, end_ts: int
) -> int:
    """Sum raw bytes overlapping [start_ts, end_ts) with hour-bucket proration."""
    if int(end_ts) <= int(start_ts):
        return 0
    lo = _align_down_hour(int(start_ts))
    hi = _align_up_hour(int(end_ts))
    rows = (
        await db.execute(
            select(UsageHourBucket.hour_ts, UsageHourBucket.raw_bytes).where(
                UsageHourBucket.user_id == int(user_id),
                UsageHourBucket.hour_ts >= lo,
                UsageHourBucket.hour_ts < hi,
            )
        )
    ).all()
    total = 0
    for ts, raw in rows:
        total += _overlap_bytes(int(ts), int(raw or 0), int(start_ts), int(end_ts))
    return int(total)


def _pending_bytes(
    *,
    live: int,
    cursor: int,
    seeded: bool,
) -> int:
    """Unsampled live delta — only after cursor has been seeded."""
    if not seeded:
        return 0
    return max(0, int(live) - int(cursor))


async def _hour_rows(
    db: AsyncSession, user_id: int, start_ts: int, end_ts: int
) -> list[UsageHourBucket]:
    res = await db.execute(
        select(UsageHourBucket)
        .where(
            UsageHourBucket.user_id == int(user_id),
            UsageHourBucket.hour_ts >= int(start_ts),
            UsageHourBucket.hour_ts < int(end_ts),
        )
        .order_by(UsageHourBucket.hour_ts.asc())
    )
    return list(res.scalars().all())


def _pct_change(cur: int | float, prev: int | float) -> float | None:
    p = float(prev or 0)
    c = float(cur or 0)
    if p <= 0:
        return None if c <= 0 else 100.0
    return round(((c - p) / p) * 100.0, 1)


async def build_user_analytics(
    db: AsyncSession,
    user: User,
    *,
    tz_offset_min: int = 0,
) -> dict[str, Any]:
    uid = int(user.id)
    now = _utc_now()
    periods = _period_starts(now, tz_offset_min)
    hour = int(periods["hour"])
    day = int(periods["day"])
    week = int(periods["week"])
    month = int(periods["month"])
    next_month_ts = int(periods["next_month"])
    prev_month = int(periods["prev_month"])
    tz_off = int(periods["tz_offset_min"])

    state = await db.get(UserUsageState, uid)
    online = bool(state and state.online_since is not None)
    online_since = (
        state.online_since.isoformat() if state and state.online_since else None
    )
    last_sub = _as_utc(state.last_sub_fetch_at) if state else None
    last_sample = _as_utc(state.last_sample_at) if state else None
    hour_end = int(periods["hour_end"])
    day_end = int(periods["day_end"])
    week_end = int(periods["week_end"])
    grid_hour = int(periods["grid_hour"])

    # Live raw from accounts (authoritative) vs last sample cursor
    live_raw = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(PanelAccount.used_traffic), 0)).where(
                    PanelAccount.user_id == uid,
                    PanelAccount.status.in_(
                        (
                            PanelAccountStatus.active,
                            PanelAccountStatus.error,
                            PanelAccountStatus.pending,
                            PanelAccountStatus.deleted,
                        )
                    ),
                )
            )
        ).scalar()
        or 0
    )
    cursor = int(state.last_raw_total or 0) if state else 0
    seeded = bool(state and state.last_sample_at is not None)
    pending = _pending_bytes(live=live_raw, cursor=cursor, seeded=seeded)

    # Periods (exact local bounds + proration) + pending only if seeded
    cur_hour = (await _sum_hours(db, uid, hour, hour_end)) + pending
    prev_hour = await _sum_hours(db, uid, hour - 3600, hour)
    cur_day = (await _sum_hours(db, uid, day, day_end)) + pending
    prev_day = await _sum_hours(db, uid, day - 86400, day)
    cur_week = (await _sum_hours(db, uid, week, week_end)) + pending
    prev_week = await _sum_hours(db, uid, week - 7 * 86400, week)
    cur_month = (await _sum_hours(db, uid, month, next_month_ts)) + pending
    prev_month_bytes = await _sum_hours(db, uid, prev_month, month)

    # Last 48h series on UTC hour grid (labels converted locally in UI)
    series_start = grid_hour - 47 * 3600
    buckets = await _hour_rows(db, uid, series_start, grid_hour + 3600)
    by_h = {int(b.hour_ts): b for b in buckets}
    hourly = []
    for i in range(48):
        ts = series_start + i * 3600
        b = by_h.get(ts)
        raw = int(b.raw_bytes or 0) if b else 0
        if i == 47:
            raw += pending
        act = int(b.active_seconds or 0) if b else 0
        hourly.append(
            {
                "hour_ts": ts,
                "raw_bytes": raw,
                "active_seconds": act,
            }
        )

    # Day-of heatmap: last 14 days by client local hour-of-day
    heat_start = grid_hour - 14 * 86400
    heat_rows = await _hour_rows(db, uid, heat_start, grid_hour + 3600)
    by_hod = [0] * 24
    for b in heat_rows:
        by_hod[_hod_index(int(b.hour_ts), tz_off)] += int(b.raw_bytes or 0)
    if pending:
        by_hod[_hod_index(grid_hour, tz_off)] += pending
    peak_hod = max(range(24), key=lambda h: by_hod[h]) if any(by_hod) else None

    # Sessions
    sessions = list(
        (
            await db.execute(
                select(UsageSession)
                .where(UsageSession.user_id == uid)
                .order_by(UsageSession.started_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    current_session = None
    prev_session = None
    closed = [s for s in sessions if not s.open]
    open_s = next((s for s in sessions if s.open), None)
    if open_s and state and state.online_since is not None:
        used = max(0, live_raw - int(state.online_raw_at_start or 0))
        started = _as_utc(open_s.started_at) or now
        dur = max(1.0, (now - started).total_seconds())
        current_session = {
            "started_at": started.isoformat(),
            "duration_seconds": int(dur),
            "raw_bytes": used,
            "open": True,
        }
    if closed:
        s = closed[0]
        dur = 1.0
        if s.ended_at and s.started_at:
            dur = max(1.0, (_as_utc(s.ended_at) - _as_utc(s.started_at)).total_seconds())
        prev_session = {
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "ended_at": s.ended_at.isoformat() if s.ended_at else None,
            "duration_seconds": int(dur),
            "raw_bytes": int(s.raw_bytes or 0),
            "open": False,
        }

    session_vs_prev = None
    if current_session and prev_session:
        session_vs_prev = {
            "bytes_change_pct": _pct_change(
                current_session["raw_bytes"], prev_session["raw_bytes"]
            ),
        }

    devices = list(
        (
            await db.execute(
                select(SubClientProfile)
                .where(
                    SubClientProfile.user_id == uid,
                    SubClientProfile.kind == "device",
                )
                .order_by(SubClientProfile.last_seen_at.desc())
            )
        )
        .scalars()
        .all()
    )
    networks = list(
        (
            await db.execute(
                select(SubClientProfile)
                .where(
                    SubClientProfile.user_id == uid,
                    SubClientProfile.kind == "network",
                )
                .order_by(SubClientProfile.last_seen_at.desc())
            )
        )
        .scalars()
        .all()
    )
    recent_fetches = list(
        (
            await db.execute(
                select(SubFetchEvent)
                .where(SubFetchEvent.user_id == uid)
                .order_by(SubFetchEvent.ts.desc())
                .limit(30)
            )
        )
        .scalars()
        .all()
    )

    raw_24h = await _sum_hours(db, uid, hour - 23 * 3600, hour + 3600)
    health = _health_label(
        status=user.status,
        raw_24h=raw_24h,
        raw_1h=cur_hour,
        last_sub=last_sub,
        last_traffic=last_sample if (state and int(state.last_raw_total or 0) > 0) else None,
        device_count=len(devices),
        network_count=len(networks),
        online=online,
        created_at=_as_utc(user.created_at),
    )

    def _ser_profile(p: SubClientProfile) -> dict[str, Any]:
        return {
            "key": p.key,
            "label": p.label,
            "detail": p.detail,
            "hit_count": int(p.hit_count or 0),
            "first_seen_at": p.first_seen_at.isoformat() if p.first_seen_at else None,
            "last_seen_at": p.last_seen_at.isoformat() if p.last_seen_at else None,
        }

    return {
        "username": user.username,
        "display_name": user.display_name,
        "status": getattr(user.status, "value", user.status),
        "health": health,
        "online": online,
        "online_since": online_since,
        "last_sub_fetch_at": last_sub.isoformat() if last_sub else None,
        "last_sample_at": last_sample.isoformat() if last_sample else None,
        "tz_offset_min": tz_off,
        "raw_totals": {
            "current_raw": live_raw,
            "lifetime_raw_cursor": cursor,
            "pending_raw": pending,
            "weighted_used": int(user.used_traffic or 0),
            "note": "مصرف‌ها خام و بدون ضریب‌اند؛ بازه‌ها با ساعت محلی دستگاه",
        },
        "compare": {
            "hour": {
                "current": cur_hour,
                "previous": prev_hour,
                "change_pct": _pct_change(cur_hour, prev_hour),
            },
            "day": {
                "current": cur_day,
                "previous": prev_day,
                "change_pct": _pct_change(cur_day, prev_day),
            },
            "week": {
                "current": cur_week,
                "previous": prev_week,
                "change_pct": _pct_change(cur_week, prev_week),
            },
            "month": {
                "current": cur_month,
                "previous": prev_month_bytes,
                "change_pct": _pct_change(cur_month, prev_month_bytes),
            },
        },
        "current_session": current_session,
        "previous_session": prev_session,
        "session_vs_previous": session_vs_prev,
        "hourly": hourly,
        "hour_of_day": {
            "bytes": by_hod,
            "peak_hour": peak_hod,
            "peak_bytes": by_hod[peak_hod] if peak_hod is not None else 0,
        },
        "devices": {
            "count": len(devices),
            "items": [_ser_profile(p) for p in devices],
        },
        "networks": {
            "count": len(networks),
            "items": [_ser_profile(p) for p in networks],
        },
        "recent_fetches": [
            {
                "ts": e.ts.isoformat() if e.ts else None,
                "ip": e.client_ip,
                "network": e.network_key,
                "app": e.client_app,
                "country": e.country,
                "ua": e.user_agent,
            }
            for e in recent_fetches
        ],
        "sessions": [
            {
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                "raw_bytes": int(s.raw_bytes or 0),
                "peak_bps": float(s.peak_bps or 0),
                "avg_bps": float(s.avg_bps or 0),
                "open": bool(s.open),
            }
            for s in sessions[:12]
        ],
    }


async def build_overview(
    db: AsyncSession,
    *,
    limit_users: int = 40,
    tz_offset_min: int = 0,
) -> dict[str, Any]:
    now = _utc_now()
    periods = _period_starts(now, tz_offset_min)
    hour = int(periods["hour"])
    hour_end = int(periods["hour_end"])
    day = int(periods["day"])
    day_end = int(periods["day_end"])
    grid_hour = int(periods["grid_hour"])
    tz_off = int(periods["tz_offset_min"])

    async def _sum_all(start_ts: int, end_ts: int) -> int:
        if end_ts <= start_ts:
            return 0
        lo = _align_down_hour(start_ts)
        hi = _align_up_hour(end_ts)
        rows = (
            await db.execute(
                select(UsageHourBucket.hour_ts, UsageHourBucket.raw_bytes).where(
                    UsageHourBucket.hour_ts >= lo,
                    UsageHourBucket.hour_ts < hi,
                )
            )
        ).all()
        return sum(
            _overlap_bytes(int(ts), int(raw or 0), start_ts, end_ts)
            for ts, raw in rows
        )

    cur = await _sum_all(hour, hour_end)
    prev = await _sum_all(hour - 3600, hour)
    today = await _sum_all(day, day_end)

    # Global pending from seeded users only (never dump unseeded lifetime)
    live_all = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(PanelAccount.used_traffic), 0)).where(
                    PanelAccount.status.in_(
                        (
                            PanelAccountStatus.active,
                            PanelAccountStatus.error,
                            PanelAccountStatus.pending,
                            PanelAccountStatus.deleted,
                        )
                    )
                )
            )
        ).scalar()
        or 0
    )
    cursor_all = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(UserUsageState.last_raw_total), 0)).where(
                    UserUsageState.last_sample_at.is_not(None)
                )
            )
        ).scalar()
        or 0
    )
    pending_all = max(0, live_all - cursor_all)
    cur += pending_all
    today += pending_all

    online_n = int(
        (
            await db.execute(
                select(func.count())
                .select_from(UserUsageState)
                .where(UserUsageState.online_since.is_not(None))
            )
        ).scalar()
        or 0
    )

    # Peak hour-of-day across all users (last 7d) — client local clock
    heat_start = grid_hour - 7 * 86400
    rows = (
        await db.execute(
            select(UsageHourBucket.hour_ts, UsageHourBucket.raw_bytes).where(
                UsageHourBucket.hour_ts >= heat_start
            )
        )
    ).all()
    by_hod = [0] * 24
    for ts, raw in rows:
        by_hod[_hod_index(int(ts), tz_off)] += int(raw or 0)
    if pending_all:
        by_hod[_hod_index(grid_hour, tz_off)] += pending_all
    peak_hod = max(range(24), key=lambda h: by_hod[h]) if any(by_hod) else None

    # Top consumers this local hour (overlap-aware via grid + pending later in UI)
    top_lo = _align_down_hour(hour)
    top_hi = _align_up_hour(hour_end)
    top_rows = (
        await db.execute(
            select(
                UsageHourBucket.user_id,
                UsageHourBucket.hour_ts,
                UsageHourBucket.raw_bytes,
                User.username,
            )
            .join(User, User.id == UsageHourBucket.user_id)
            .where(
                UsageHourBucket.hour_ts >= top_lo,
                UsageHourBucket.hour_ts < top_hi,
            )
        )
    ).all()
    top_acc: dict[int, dict[str, Any]] = {}
    for uid, ts, raw, uname in top_rows:
        part = _overlap_bytes(int(ts), int(raw or 0), hour, hour_end)
        if part <= 0:
            continue
        slot = top_acc.setdefault(
            int(uid), {"username": uname, "user_id": int(uid), "raw_bytes": 0}
        )
        slot["raw_bytes"] += part
    # Add per-user pending for seeded users
    if pending_all > 0:
        live_by_user = {
            int(uid): int(live or 0)
            for uid, live in (
                await db.execute(
                    select(
                        PanelAccount.user_id,
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
                    .group_by(PanelAccount.user_id)
                )
            ).all()
        }
        state_rows = (
            await db.execute(
                select(
                    UserUsageState.user_id,
                    UserUsageState.last_raw_total,
                    User.username,
                )
                .join(User, User.id == UserUsageState.user_id)
                .where(UserUsageState.last_sample_at.is_not(None))
            )
        ).all()
        for uid, curs, uname in state_rows:
            pend = _pending_bytes(
                live=int(live_by_user.get(int(uid), 0)),
                cursor=int(curs or 0),
                seeded=True,
            )
            if pend <= 0:
                continue
            slot = top_acc.setdefault(
                int(uid), {"username": uname, "user_id": int(uid), "raw_bytes": 0}
            )
            slot["raw_bytes"] += pend
    top = sorted(top_acc.values(), key=lambda x: -int(x["raw_bytes"]))[:limit_users]

    # Batch health for recent users (no N+1)
    users = list(
        (
            await db.execute(select(User).order_by(User.id.desc()).limit(400))
        )
        .scalars()
        .all()
    )
    uids = [int(u.id) for u in users]
    states = {
        int(s.user_id): s
        for s in (
            await db.execute(
                select(UserUsageState).where(UserUsageState.user_id.in_(uids))
            )
        ).scalars().all()
    } if uids else {}

    raw_1h_map: dict[int, int] = {i: 0 for i in uids}
    raw_24h_map: dict[int, int] = {i: 0 for i in uids}
    if uids:
        for uid, ts, raw in (
            await db.execute(
                select(
                    UsageHourBucket.user_id,
                    UsageHourBucket.hour_ts,
                    UsageHourBucket.raw_bytes,
                ).where(
                    UsageHourBucket.user_id.in_(uids),
                    UsageHourBucket.hour_ts >= _align_down_hour(hour),
                    UsageHourBucket.hour_ts < _align_up_hour(hour_end),
                )
            )
        ).all():
            raw_1h_map[int(uid)] = raw_1h_map.get(int(uid), 0) + _overlap_bytes(
                int(ts), int(raw or 0), hour, hour_end
            )
        for uid, raw in (
            await db.execute(
                select(
                    UsageHourBucket.user_id,
                    func.coalesce(func.sum(UsageHourBucket.raw_bytes), 0),
                )
                .where(
                    UsageHourBucket.user_id.in_(uids),
                    UsageHourBucket.hour_ts >= grid_hour - 23 * 3600,
                )
                .group_by(UsageHourBucket.user_id)
            )
        ).all():
            raw_24h_map[int(uid)] = int(raw or 0)

    device_counts: dict[int, int] = {i: 0 for i in uids}
    network_counts: dict[int, int] = {i: 0 for i in uids}
    if uids:
        for uid, kind, cnt in (
            await db.execute(
                select(
                    SubClientProfile.user_id,
                    SubClientProfile.kind,
                    func.count(),
                )
                .where(SubClientProfile.user_id.in_(uids))
                .group_by(SubClientProfile.user_id, SubClientProfile.kind)
            )
        ).all():
            if kind == "device":
                device_counts[int(uid)] = int(cnt or 0)
            elif kind == "network":
                network_counts[int(uid)] = int(cnt or 0)

    health_counts: dict[str, int] = {}
    health_users: list[dict[str, Any]] = []
    for u in users:
        uid = int(u.id)
        st = states.get(uid)
        raw_1h = raw_1h_map.get(uid, 0)
        raw_24h = raw_24h_map.get(uid, 0)
        d_count = device_counts.get(uid, 0)
        n_count = network_counts.get(uid, 0)
        h = _health_label(
            status=u.status,
            raw_24h=raw_24h,
            raw_1h=raw_1h,
            last_sub=_as_utc(st.last_sub_fetch_at) if st else None,
            last_traffic=_as_utc(st.last_sample_at) if st else None,
            device_count=d_count,
            network_count=n_count,
            online=bool(st and st.online_since is not None),
            created_at=_as_utc(u.created_at),
        )
        health_counts[h["code"]] = health_counts.get(h["code"], 0) + 1
        health_users.append(
            {
                "username": u.username,
                "display_name": u.display_name,
                "health": h,
                "raw_1h": raw_1h,
                "raw_24h": raw_24h,
                "devices": d_count,
                "networks": n_count,
                "online": bool(st and st.online_since is not None),
            }
        )

    order = {
        "struggling": 0,
        "multi_client": 1,
        "never_used": 2,
        "idle": 3,
        "quota_done": 4,
        "healthy": 5,
        "ok_quiet": 6,
        "low": 7,
    }
    health_users.sort(key=lambda x: (order.get(x["health"]["code"], 9), -x["raw_1h"]))

    # Global hourly last 24h (UTC grid; UI labels locally)
    g_start = grid_hour - 23 * 3600
    g_rows = (
        await db.execute(
            select(
                UsageHourBucket.hour_ts,
                func.coalesce(func.sum(UsageHourBucket.raw_bytes), 0),
            )
            .where(UsageHourBucket.hour_ts >= g_start)
            .group_by(UsageHourBucket.hour_ts)
            .order_by(UsageHourBucket.hour_ts.asc())
        )
    ).all()
    g_map = {int(t): int(v or 0) for t, v in g_rows}
    global_hourly = []
    for i in range(24):
        ts = g_start + i * 3600
        raw = g_map.get(ts, 0)
        if i == 23:
            raw += pending_all
        global_hourly.append({"hour_ts": ts, "raw_bytes": raw})

    return {
        "generated_at": now.isoformat(),
        "tz_offset_min": tz_off,
        "online_users": online_n,
        "raw_hour": {
            "current": cur,
            "previous": prev,
            "change_pct": _pct_change(cur, prev),
        },
        "raw_today": today,
        "raw_current": live_all,
        "raw_pending": pending_all,
        "hour_of_day": {
            "bytes": by_hod,
            "peak_hour": peak_hod,
            "peak_bytes": by_hod[peak_hod] if peak_hod is not None else 0,
        },
        "global_hourly": global_hourly,
        "top_hour": [
            {
                "username": t["username"],
                "user_id": int(t["user_id"]),
                "raw_bytes": int(t["raw_bytes"] or 0),
            }
            for t in top
            if int(t["raw_bytes"] or 0) > 0
        ],
        "health_counts": health_counts,
        "health_users": health_users[:limit_users],
        "note": "مصرف خام بدون ضریب · بازه‌ها با زمان محلی و تناسب سطل ساعتی",
    }
