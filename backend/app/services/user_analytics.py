"""Collect and query per-user analytics (raw usage, sessions, sub clients)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserStatus
from app.models.user_analytics import (
    SubClientProfile,
    SubFetchEvent,
    UsageHourBucket,
    UsageSession,
    UserUsageState,
)
from app.services.client_ua import (
    ACCEPT_CH_HEADERS as _ACCEPT_CH_HEADERS,
    KNOWN_APP_NAMES as _KNOWN_APP_NAMES,
    detect_client_channel,
    device_label as _device_label,
    parse_browser_name,
    parse_client_app,
    parse_client_device,
    parse_client_os,
    parse_device_brand,
    parse_device_model,
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
# (user_id, device_key, network_key) → monotonic ts — network switches always log
_sub_log_last: dict[tuple[int, str, str], float] = {}
_SUB_LOG_DEBOUNCE = 45.0  # seconds per same device + same network
_sample_lock = asyncio.Lock()


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


# Iran-edge / tunnel peers — never treat as the end-user client IP
_INFRA_IPS = {
    "5.202.8.221",  # primary Iran sub edge
    "85.9.105.116",  # secondary Iran edge
}


def _ser_sub_fetch(e: SubFetchEvent) -> dict[str, Any]:
    from app.services.iran_operators import classify_operator, operator_label

    ua = e.user_agent or ""
    # Re-parse live so older rows pick up better brand/model/OS/browser labels
    info = parse_client_device(ua)
    stored_brand = (e.device_brand or "").strip()
    stored_model = (getattr(e, "device_model", None) or "").strip()
    stored_os = (getattr(e, "client_os", None) or "").strip()
    brand = info["brand"]
    model = info["model"] or (stored_model or None)
    # Prefer richer stored brand when live parse is generic
    if stored_brand and brand in ("Android", "سایر", "نامشخص"):
        if stored_brand not in ("Android", "سایر", "نامشخص"):
            brand = stored_brand
    app = info["app"] or e.client_app or parse_client_app(ua)
    # Prefer stored OS (may include Client-Hint version from fetch time)
    os_name = stored_os or info["os"]
    # If stored is bare "Android" but live parse found a plausible version, use live
    if stored_os in ("Android", "iOS", "iPadOS", "macOS", "Windows") and info[
        "os"
    ] not in (stored_os, "نامشخص"):
        if any(ch.isdigit() for ch in info["os"]):
            os_name = info["os"]
    channel = info["channel"]
    browser = info["browser"]
    label = _device_label(brand=brand, model=model, os_name=os_name)
    op = (e.operator or "").strip() or "other"
    ip = (e.client_ip or "").strip()
    ip_ok = _ip_usable(ip)
    if not ip_ok:
        op_label = "لوکال / تشخیص‌نشده"
        op = "other"
        ip_out = None
    else:
        # Re-classify from IP: Iranian ISP, or VPN/خارج for foreign public exits
        live = classify_operator(ip)
        if live != "other":
            op = live
        elif (op or "").strip().lower() in ("", "other", "isp"):
            # Should not happen for public IPs (classify → vpn); keep safe
            op = "other"
        op_label = operator_label(op)
        ip_out = ip
    return {
        "ts": e.ts.isoformat() if e.ts else None,
        "ip": ip_out,
        "network": e.network_key if ip_ok else "local",
        "app": app,
        "browser": browser,
        "country": e.country,
        "ua": ua,
        "operator": op,
        "operator_label": op_label,
        "device_brand": brand,
        "device_model": model,
        "device_label": label,
        "os": os_name,
        "channel": channel,
        "channel_label": (
            "اپلیکیشن"
            if channel == "app"
            else "مرورگر"
            if channel == "browser"
            else "نامشخص"
        ),
        "ip_ok": ip_ok,
    }


def _best_sub_client(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Latest real sub open/update (public IP) — always by time, not old ISP bias.

    Returns ``None`` when every row lacks a usable public IP so the UI does
    not keep showing stale «تشخیص‌نشده / 127.0.0.1» from admin previews.
    """
    if not items:
        return None
    usable = [row for row in items if row.get("ip_ok")]
    if not usable:
        return None

    def sort_key(row: dict[str, Any]) -> tuple:
        from app.services.iran_operators import is_known_operator

        ts = str(row.get("ts") or "")
        op = (row.get("operator") or "other").strip()
        # Tie-break only when timestamps match: prefer known ISP / app channel
        known = (
            2
            if op in ("irancell", "mci", "rightel")
            else 1
            if is_known_operator(op)
            else 0
        )
        app = 1 if row.get("channel") == "app" else 0
        return (ts, known, app)

    return max(usable, key=sort_key)


def _profile_is_local_junk(p: SubClientProfile) -> bool:
    key = (p.key or "").strip().lower()
    detail = (p.detail or "").strip().lower()
    label = (p.label or "").strip()
    if key in ("local",):
        return True
    if "127.0.0.1" in detail or "::1" in detail:
        return True
    if key == "other" and (
        "لوکال" in label
        or "تشخیص" in label
        or detail in ("", "unknown", "127.0.0.1 · browser", "127.0.0.1 · app", "127.0.0.1 · unknown")
        or detail.startswith("127.0.0.1")
        or detail.startswith("unknown")
    ):
        return True
    return False


def _device_key(client_app: str, user_agent: str) -> str:
    # Same app + UA family ≈ same device class (multi-phone of same app still distinct UA)
    base = f"{client_app}|{(user_agent or '')[:180]}".lower()
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()[:20]


def _ip_usable(ip: str | None) -> bool:
    """True for a routable public client IP (not loopback/private/unknown)."""
    raw = (ip or "").strip().split(",")[0].strip()
    if not raw or raw.lower() in ("unknown", "null", "none", "-"):
        return False
    if raw in _INFRA_IPS:
        return False
    try:
        import ipaddress

        addr = ipaddress.ip_address(raw)
    except ValueError:
        return False
    return not (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _ip_non_loopback(ip: str | None) -> bool:
    raw = (ip or "").strip().split(",")[0].strip()
    if not raw or raw.lower() in ("unknown", "null", "none", "-", "127.0.0.1", "::1"):
        return False
    try:
        import ipaddress

        return not ipaddress.ip_address(raw).is_loopback
    except ValueError:
        return False


def _network_key(ip: str) -> str:
    ip = (ip or "").strip()
    if not ip or not _ip_non_loopback(ip):
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
    """Best-effort public client IP behind nginx / CF / Iran edge.

    Skips loopback and private addresses so operator detection is not polluted
    by ``127.0.0.1`` from local proxies or admin previews.
    """

    def _h(name: str) -> str:
        for k, v in headers.items():
            if k.lower() == name.lower():
                return (v or "").strip()
        return ""

    candidates: list[str] = []
    # Prefer X-Astra-Client-IP first: Iran edge sets the real subscriber IP.
    # CF-Connecting-IP through our topology is often the edge itself
    # (5.202.8.221), which would wrongly classify everyone as one ISP and
    # freeze sub order to global/pishgaman.
    for key in (
        "x-astra-client-ip",
        "true-client-ip",
        "cf-connecting-ip",
        "x-real-ip",
        "x-client-ip",
        "x-forwarded-for",
    ):
        v = _h(key)
        if not v:
            continue
        for part in v.split(","):
            p = part.strip()
            if p:
                candidates.append(p)
    if fallback:
        fb = str(fallback).strip()
        if fb:
            candidates.append(fb)

    for ip in candidates:
        if _ip_usable(ip):
            return ip
    for ip in candidates:
        if _ip_non_loopback(ip):
            return ip
    return "unknown"


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
    """Ingest one usage-total sample for a user (idempotent per call).

    ``raw_total`` is the live Astra usage counter (weighted Σ raw×coeff —
    same number as the users page).
    """
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

    # Source/scale jump (bulk panel sync, raw→weighted, cursor heal): rebase
    # ONLY. Never credit hour/day buckets — that invented multi-TB «این ساعت».
    # Cap ≈ 80Mbps sustained over the gap (was 500Mbps and still too loose).
    max_natural = max(80 * 1024 * 1024, int(dt * (80_000_000 / 8)))
    if delta > max_natural:
        state.last_raw_total = raw
        state.last_sample_at = now
        return

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


_weighted_rebase_done = False


async def rebase_cursors_to_weighted_usage() -> dict[str, int]:
    """Align analytics cursors with ``User.used_traffic`` (weighted).

    Old sampler tracked Σ PanelAccount.used_traffic (incl. deleted). Switching
    without rebase dumps the scale gap into «این ساعت». Idempotent when already
    aligned (~5%).
    """
    global _weighted_rebase_done
    if _weighted_rebase_done:
        return {"skipped": 1}
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        live_all = int(
            (
                await db.execute(
                    select(func.coalesce(func.sum(User.used_traffic), 0))
                )
            ).scalar()
            or 0
        )
        cursor_all = int(
            (
                await db.execute(
                    select(
                        func.coalesce(func.sum(UserUsageState.last_raw_total), 0)
                    ).where(UserUsageState.last_sample_at.is_not(None))
                )
            ).scalar()
            or 0
        )
        denom = max(live_all, cursor_all, 1)
        gap_ratio = abs(live_all - cursor_all) / float(denom)
        if cursor_all > 0 and gap_ratio < 0.05:
            _weighted_rebase_done = True
            return {"aligned": 1, "live": live_all, "cursor": cursor_all}

        now = _utc_now()
        hour_ts = _hour_ts(now)
        day_ts = int(
            now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        )
        users = (
            await db.execute(
                select(User.id, func.coalesce(User.used_traffic, 0))
            )
        ).all()
        n = 0
        for uid, total in users:
            state = await _get_or_create_state(db, int(uid))
            live = int(total or 0)
            state.last_raw_total = live
            state.last_sample_at = now
            state.period_hour_ts = hour_ts
            state.period_hour_raw = live
            state.period_day_ts = day_ts
            state.period_day_raw = live
            n += 1
            if n % 80 == 0:
                await db.commit()

        # Clear polluted current-hour buckets so «این ساعت» starts clean
        cleared = await db.execute(
            delete(UsageHourBucket).where(UsageHourBucket.hour_ts >= hour_ts)
        )
        await db.commit()
        _weighted_rebase_done = True
        logger.info(
            "analytics cursors rebased to weighted: users=%s live=%s old_cursor=%s "
            "cleared_hours=%s",
            n,
            live_all,
            cursor_all,
            cleared.rowcount if cleared is not None else 0,
        )
        return {
            "rebased": n,
            "live": live_all,
            "old_cursor": cursor_all,
            "cleared_hour_rows": int(cleared.rowcount or 0)
            if cleared is not None
            else 0,
        }


async def align_analytics_cursors(user_ids: list[int]) -> int:
    """Set analytics cursors to current ``User.used_traffic`` without crediting hours.

    Call after bulk panel usage pulls so the next sample does not treat the
    catch-up jump as organic «این ساعت» traffic.
    """
    ids = list(dict.fromkeys(int(x) for x in user_ids if x))
    if not ids:
        return 0
    from app.core.database import AsyncSessionLocal

    now = _utc_now()
    n = 0
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(User.id, func.coalesce(User.used_traffic, 0)).where(
                    User.id.in_(ids)
                )
            )
        ).all()
        for uid, total in rows:
            state = await _get_or_create_state(db, int(uid))
            state.last_raw_total = max(0, int(total or 0))
            state.last_sample_at = now
            n += 1
            if n % 80 == 0:
                await db.commit()
        await db.commit()
    return n


async def scrub_inflated_hour_buckets(
    *, since_hour_ts: int, min_user_bytes: int = 1024**3
) -> dict[str, int]:
    """Zero absurd per-user hour rows (bulk-sync dumps), keep small organic ones."""
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import update

    async with AsyncSessionLocal() as db:
        # Zero giant rows
        big = await db.execute(
            update(UsageHourBucket)
            .where(
                UsageHourBucket.hour_ts >= int(since_hour_ts),
                UsageHourBucket.raw_bytes >= int(min_user_bytes),
            )
            .values(raw_bytes=0, peak_bps=0.0)
        )
        # Also zero mid junk: >200MB in those hours is almost always catch-up
        mid = await db.execute(
            update(UsageHourBucket)
            .where(
                UsageHourBucket.hour_ts >= int(since_hour_ts),
                UsageHourBucket.raw_bytes >= 200 * 1024 * 1024,
            )
            .values(raw_bytes=0, peak_bps=0.0)
        )
        await db.commit()
        return {
            "zeroed_ge_1gb": int(big.rowcount or 0),
            "zeroed_ge_200mb": int(mid.rowcount or 0),
        }


async def sample_all_users_once() -> dict[str, int]:
    """Advance analytics from each user's live Astra usage (weighted).

    Uses ``User.used_traffic`` — same counter as the users page
    (Σ raw×coeff, no superseded deleted double-count). No remote I/O.
    """
    from app.core.database import AsyncSessionLocal

    if _sample_lock.locked():
        return {"skipped": 1, "busy": 1}
    async with _sample_lock:
        t0 = time.monotonic()
        try:
            rebase = await rebase_cursors_to_weighted_usage()
            if rebase.get("rebased"):
                logger.info("analytics weighted rebase: %s", rebase)
        except Exception:  # noqa: BLE001
            logger.exception("analytics weighted rebase failed")
        # Load ids in a short read txn, then sample in small sessions so we
        # never sit ``idle in transaction`` across event-loop yields.
        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(User.id, func.coalesce(User.used_traffic, 0)).where(
                        User.status.in_(
                            (
                                UserStatus.active,
                                UserStatus.on_hold,
                                UserStatus.limited,
                            )
                        ),
                        User.used_traffic > 0,
                    )
                )
            ).all()
            await db.rollback()
        pairs = [(int(uid), int(total or 0)) for uid, total in rows]
        n = 0
        chunk = 40
        for i in range(0, len(pairs), chunk):
            batch = pairs[i : i + chunk]
            async with AsyncSessionLocal() as db:
                for uid, total in batch:
                    try:
                        async with db.begin_nested():
                            await apply_raw_sample(db, uid, total)
                        n += 1
                    except Exception:  # noqa: BLE001
                        logger.exception("analytics sample user=%s failed", uid)
                        try:
                            await db.rollback()
                        except Exception:  # noqa: BLE001
                            pass
                try:
                    await db.commit()
                except Exception:  # noqa: BLE001
                    try:
                        await db.rollback()
                    except Exception:  # noqa: BLE001
                        pass
            # Session closed → connection returned before yielding the loop.
            await asyncio.sleep(0)
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
    ip_ok = _ip_usable(ip)
    from app.services.iran_operators import classify_operator, operator_label

    info = parse_client_device(ua, headers)
    app = info["app"]
    brand = info["brand"]
    model = info["model"]
    os_name = info["os"]
    channel = info["channel"]
    dkey = _device_key(app, ua)
    nkey = _network_key(ip)

    # Browser preview without a public IP (admin open / broken edge) — skip noise
    if channel == "browser" and not ip_ok:
        return

    # Local / unknown IP → do not invent an Iranian operator
    op_code = classify_operator(ip) if ip_ok else "other"
    if not ip_ok:
        op_label = "لوکال / تشخیص‌نشده"
    else:
        op_label = operator_label(op_code)
    device_label = info["device_label"] or app
    uid = int(user_id)
    now_m = time.monotonic()
    # Include network so switching ISP/Wi‑Fi always creates a fresh event
    debounce_key = (uid, dkey, nkey)
    last = _sub_log_last.get(debounce_key)
    skip_event = last is not None and (now_m - last) < _SUB_LOG_DEBOUNCE
    if not skip_event:
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
            if not skip_event:
                db.add(
                    SubFetchEvent(
                        user_id=uid,
                        ts=now,
                        client_ip=(ip if ip_ok else "unknown")[:64],
                        network_key=nkey[:64],
                        device_key=dkey[:64],
                        client_app=(app or "")[:64],
                        user_agent=ua,
                        country=country,
                        operator=op_code[:16],
                        device_brand=(brand or "")[:64],
                        device_model=(model or "")[:96] if model else None,
                        client_os=(os_name or "")[:64] if os_name else None,
                    )
                )
            await _upsert_profile(
                db,
                uid,
                kind="device",
                key=dkey,
                label=device_label[:200],
                detail=(f"{os_name} · {channel}" if os_name else channel),
                now=now,
            )
            # Skip junk local network rows (admin preview / loopback proxy)
            if nkey != "local" and ip_ok:
                net_key = op_code if op_code != "other" else nkey
                net_label = op_label if op_code != "other" else nkey
                await _upsert_profile(
                    db,
                    uid,
                    kind="network",
                    key=net_key[:64],
                    label=net_label[:200],
                    detail=ip[:64],
                    now=now,
                )
            await _upsert_profile(
                db,
                uid,
                kind="brand",
                key=brand.lower()[:64],
                label=(info["device_label"] or brand)[:200],
                # Keep UA when model is missing so online/brand reclassify can mine it
                detail=(
                    (model[:500] if model else None)
                    or (ua[:500] if ua else None)
                ),
                now=now,
            )
            # Only store operator when we have a real public IP
            if ip_ok:
                await _upsert_profile(
                    db,
                    uid,
                    kind="operator",
                    key=op_code[:64],
                    label=op_label[:200],
                    detail=ip[:64],
                    now=now,
                )
            await _upsert_profile(
                db,
                uid,
                kind="os",
                key=os_name.lower()[:64],
                label=os_name[:200],
                detail=None,
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


def _sum_from_map(
    by_h: dict[int, int], start_ts: int, end_ts: int
) -> int:
    """Sum overlapping bytes from an in-memory hour_ts → raw map."""
    if int(end_ts) <= int(start_ts):
        return 0
    lo = _align_down_hour(int(start_ts))
    hi = _align_up_hour(int(end_ts))
    total = 0
    ts = lo
    while ts < hi:
        raw = int(by_h.get(ts, 0) or 0)
        if raw:
            total += _overlap_bytes(ts, raw, int(start_ts), int(end_ts))
        ts += 3600
    return int(total)


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


def _period_from_live_mark(
    live: int,
    *,
    mark_ts: int | None,
    mark_raw: int | None,
    period_ts: int,
) -> tuple[int, int, int]:
    live_i = max(0, int(live or 0))
    period_i = int(period_ts)
    prev_ts = int(mark_ts or 0)
    prev_raw = max(0, int(mark_raw or 0))
    if prev_ts != period_i or live_i < prev_raw:
        return 0, period_i, live_i
    return live_i - prev_raw, period_i, prev_raw


def apply_live_period_marks(
    state: UserUsageState,
    live: int,
    *,
    hour_ts: int,
    day_ts: int,
) -> tuple[int, int]:
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
    if d < h:
        d = h
    return int(h), int(d)


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


def _contiguous_ranges(
    hours: list[dict[str, Any]],
    pred,
) -> list[dict[str, Any]]:
    """Merge consecutive hour slots matching ``pred(raw_bytes)`` into ranges."""
    ranges: list[dict[str, Any]] = []
    i = 0
    n = len(hours)
    while i < n:
        raw = int(hours[i].get("raw_bytes") or 0)
        if not pred(raw):
            i += 1
            continue
        j = i
        total = 0
        while j < n and pred(int(hours[j].get("raw_bytes") or 0)):
            total += int(hours[j].get("raw_bytes") or 0)
            j += 1
        start_ts = int(hours[i]["hour_ts"])
        end_ts = int(hours[j - 1]["hour_ts"]) + 3600
        ranges.append(
            {
                "start_ts": start_ts,
                "end_ts": end_ts,
                "hours": j - i,
                "raw_bytes": int(total),
                "avg_bytes": int(total // max(1, j - i)),
            }
        )
        i = j
    ranges.sort(key=lambda r: -int(r["raw_bytes"]))
    return ranges


def _analyze_24h_window(hours_24: list[dict[str, Any]]) -> dict[str, Any]:
    """Peak / low slots and contiguous high/low ranges for last 24h."""
    hours = [
        {
            "hour_ts": int(h["hour_ts"]),
            "raw_bytes": max(0, int(h.get("raw_bytes") or 0)),
            "active_seconds": int(h.get("active_seconds") or 0),
        }
        for h in hours_24
    ]
    if not hours:
        return {
            "total_bytes": 0,
            "hours": [],
            "peak": None,
            "low": None,
            "high_ranges": [],
            "low_ranges": [],
            "quiet_hours": 0,
            "active_hours": 0,
            "top_hours": [],
            "bottom_hours": [],
        }

    total = sum(int(h["raw_bytes"]) for h in hours)
    peak_h = max(hours, key=lambda h: int(h["raw_bytes"]))
    active = [h for h in hours if int(h["raw_bytes"]) > 0]
    quiet_n = len(hours) - len(active)
    if active:
        low_h = min(active, key=lambda h: int(h["raw_bytes"]))
    else:
        low_h = min(hours, key=lambda h: int(h["hour_ts"]))

    peak_bytes = int(peak_h["raw_bytes"] or 0)
    high_thr = max(64 * 1024, int(peak_bytes * 0.55)) if peak_bytes > 0 else 0
    low_thr = (
        max(1024, min(int(peak_bytes * 0.12), 8 * 1024 * 1024))
        if peak_bytes > 0
        else 1024
    )

    high_ranges = (
        _contiguous_ranges(hours, lambda b: b >= high_thr and b > 0)
        if peak_bytes > 0
        else []
    )
    low_ranges = _contiguous_ranges(hours, lambda b: b <= low_thr)

    ranked = sorted(hours, key=lambda h: (-int(h["raw_bytes"]), int(h["hour_ts"])))
    top_hours = ranked[:5]
    bottom_active = sorted(
        active, key=lambda h: (int(h["raw_bytes"]), int(h["hour_ts"]))
    )[:5]
    bottom_hours = bottom_active if bottom_active else ranked[-5:][::-1]

    def _slot(h: dict[str, Any]) -> dict[str, Any]:
        return {
            "hour_ts": int(h["hour_ts"]),
            "raw_bytes": int(h["raw_bytes"]),
            "active_seconds": int(h.get("active_seconds") or 0),
        }

    return {
        "total_bytes": int(total),
        "hours": [_slot(h) for h in hours],
        "peak": _slot(peak_h) if peak_bytes > 0 else None,
        "low": _slot(low_h) if active else None,
        "high_ranges": high_ranges[:6],
        "low_ranges": sorted(
            low_ranges, key=lambda r: (-int(r["hours"]), int(r["start_ts"]))
        )[:6],
        "quiet_hours": int(quiet_n),
        "active_hours": len(active),
        "top_hours": [_slot(h) for h in top_hours if int(h["raw_bytes"]) > 0],
        "bottom_hours": [_slot(h) for h in bottom_hours],
    }


def _hour_over_hour(
    current: int,
    previous: int,
    *,
    hour_start: int,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Fair this-hour vs previous-hour comparison.

    Mid-hour ``current`` is incomplete; we project to a full hour and also
    expose the raw so-far numbers so the UI can show both.
    """
    cur = max(0, int(current or 0))
    prev = max(0, int(previous or 0))
    start = int(hour_start)
    now_i = int(now_ts if now_ts is not None else _utc_now().timestamp())
    elapsed = max(1, min(3600, now_i - start))
    # Avoid wild projection in the first ~90s of a new hour
    if elapsed < 90:
        projected = cur
        fair_pct = _pct_change(cur, prev) if elapsed >= 30 else None
    else:
        projected = int(round(cur * 3600.0 / float(elapsed)))
        fair_pct = _pct_change(projected, prev)
    delta = int(projected) - int(prev)
    return {
        "current": cur,
        "previous": prev,
        "projected": int(projected),
        "elapsed_seconds": int(elapsed),
        "elapsed_pct": round(elapsed * 100.0 / 3600.0, 1),
        "delta_bytes": delta,
        "change_pct": fair_pct,
        "change_pct_raw": _pct_change(cur, prev),
        "hour_start": start,
        "prev_hour_start": start - 3600,
        "direction": (
            "up" if delta > 0 else "down" if delta < 0 else "flat"
        ),
    }


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

    # Live usage = User.used_traffic (weighted Σ raw×coeff, same as users page)
    live_raw = int(user.used_traffic or 0)
    if state is None:
        state = await _get_or_create_state(db, uid)
    cursor = int(state.last_raw_total or 0)
    seeded = bool(state.last_sample_at is not None)
    pending = _pending_bytes(live=live_raw, cursor=cursor, seeded=seeded)

    # Authoritative hour/day from live watermarks
    cur_hour, cur_day = apply_live_period_marks(
        state, live_raw, hour_ts=hour, day_ts=day
    )
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.debug("user period mark commit failed", exc_info=True)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
    # One bucket pull covers month compare + 14d heatmap + 48h series
    series_start = grid_hour - 47 * 3600
    heat_start = grid_hour - 14 * 86400
    range_lo = min(prev_month, week - 7 * 86400, heat_start, series_start, hour - 3600)
    range_hi = max(next_month_ts, week_end, grid_hour + 3600, hour + 3600)
    all_buckets = await _hour_rows(db, uid, range_lo, range_hi)
    by_h_obj = {int(b.hour_ts): b for b in all_buckets}
    by_h_raw = {ts: int(b.raw_bytes or 0) for ts, b in by_h_obj.items()}

    prev_hour = min(_sum_from_map(by_h_raw, hour - 3600, hour), live_raw)
    prev_day = min(_sum_from_map(by_h_raw, day - 86400, day), live_raw)
    cur_week = min(_sum_from_map(by_h_raw, week, week_end) + pending, live_raw)
    prev_week = min(_sum_from_map(by_h_raw, week - 7 * 86400, week), live_raw)
    cur_month = min(
        _sum_from_map(by_h_raw, month, next_month_ts) + pending, live_raw
    )
    prev_month_bytes = min(_sum_from_map(by_h_raw, prev_month, month), live_raw)
    raw_24h = min(_sum_from_map(by_h_raw, hour - 23 * 3600, hour + 3600), live_raw)

    # Last 48h series on UTC hour grid (labels converted locally in UI)
    hourly = []
    for i in range(48):
        ts = series_start + i * 3600
        b = by_h_obj.get(ts)
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
    by_hod = [0] * 24
    for ts, raw in by_h_raw.items():
        if ts < heat_start or ts >= grid_hour + 3600:
            continue
        by_hod[_hod_index(int(ts), tz_off)] += int(raw or 0)
    if pending:
        by_hod[_hod_index(grid_hour, tz_off)] += pending
    peak_hod = max(range(24), key=lambda h: by_hod[h]) if any(by_hod) else None

    # Sessions + sub profiles / fetches (same session — sequential but fewer roundtrips overall)
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

    profiles = list(
        (
            await db.execute(
                select(SubClientProfile)
                .where(SubClientProfile.user_id == uid)
                .order_by(SubClientProfile.last_seen_at.desc())
            )
        )
        .scalars()
        .all()
    )
    devices = [p for p in profiles if p.kind == "device"]
    networks = [
        p for p in profiles if p.kind == "network" and not _profile_is_local_junk(p)
    ]
    brands = [p for p in profiles if p.kind == "brand"]
    operators = [
        p for p in profiles if p.kind == "operator" and not _profile_is_local_junk(p)
    ]
    os_profiles = [p for p in profiles if p.kind == "os"]
    # Drop leftover local junk profiles so the UI stays clean
    junk = [
        p
        for p in profiles
        if p.kind in ("operator", "network") and _profile_is_local_junk(p)
    ]
    for p in junk:
        try:
            await db.delete(p)
        except Exception:  # noqa: BLE001
            pass
    if junk:
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
    recent_fetches = list(
        (
            await db.execute(
                select(SubFetchEvent)
                .where(SubFetchEvent.user_id == uid)
                .order_by(SubFetchEvent.ts.desc())
                .limit(40)
            )
        )
        .scalars()
        .all()
    )
    fetch_items = [_ser_sub_fetch(e) for e in recent_fetches]
    last_sub_client = _best_sub_client(fetch_items)
    # Timestamp shown next to the card must match the fetch we picked (newest)
    if last_sub_client and last_sub_client.get("ts"):
        try:
            shown_ts = datetime.fromisoformat(
                str(last_sub_client["ts"]).replace("Z", "+00:00")
            )
            if shown_ts.tzinfo is None:
                shown_ts = shown_ts.replace(tzinfo=timezone.utc)
            last_sub = _as_utc(shown_ts) or last_sub
        except Exception:  # noqa: BLE001
            pass
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
        from app.services.iran_operators import (
            classify_operator,
            looks_like_generic_isp_label,
            operator_label,
        )

        key = (p.key or "").strip()
        label = p.label
        detail = (p.detail or "").strip()
        ip_guess = detail.split("·")[0].strip().split()[0] if detail else ""
        # Refresh stale «other» / bare «isp» / نامشخص → ISP یا VPN از روی IP
        if p.kind in ("operator", "network"):
            live = classify_operator(ip_guess) if ip_guess else "other"
            if live not in ("", "other"):
                key = live
                label = operator_label(live)
            elif looks_like_generic_isp_label(label) or key.lower() in (
                "",
                "other",
                "isp",
                "نامشخص",
            ):
                key = "other"
                label = operator_label("other")
        return {
            "key": key,
            "label": label,
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
        "last_sub_client": last_sub_client,
        "last_sample_at": last_sample.isoformat() if last_sample else None,
        "tz_offset_min": tz_off,
        "raw_totals": {
            "current_raw": live_raw,
            "lifetime_raw_cursor": cursor,
            "pending_raw": pending,
            "weighted_used": live_raw,
            "note": "مصرف‌ها با ضریب و مثل صفحه کاربران؛ بازه‌ها با ساعت محلی دستگاه",
        },
        "compare": {
            "hour": {
                **_hour_over_hour(
                    cur_hour,
                    prev_hour,
                    hour_start=hour,
                    now_ts=int(now.timestamp()),
                ),
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
        # Exact last-24h window (UTC hour grid; UI labels in local time)
        "window_24h": _analyze_24h_window(hourly[-24:] if hourly else []),
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
        "brands": {
            "count": len(brands),
            "items": [_ser_profile(p) for p in brands],
        },
        "operators": {
            "count": len(operators),
            "items": [_ser_profile(p) for p in operators],
        },
        "operating_systems": {
            "count": len(os_profiles),
            "items": [_ser_profile(p) for p in os_profiles],
        },
        "recent_fetches": fetch_items,
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


def _sub_event_no_public_ip():
    """True when client_ip is missing / loopback / unknown (not a real ISP)."""
    ip = func.coalesce(SubFetchEvent.client_ip, "")
    private_172 = or_(*[ip.like(f"172.{n}.%") for n in range(16, 32)])
    return or_(
        ip == "",
        ip.in_(("127.0.0.1", "::1", "unknown", "null", "none", "-")),
        ip.like("10.%"),
        ip.like("192.168.%"),
        private_172,
    )


async def build_sub_insights(
    db: AsyncSession,
    *,
    days: int = 7,
    limit: int = 16,
) -> dict[str, Any]:
    """Global subscription open/update rankings across all users.

    Fast SQL aggregates on ``sub_fetch_events`` — hits + distinct users.
    Operator ranking ignores loopback/unknown IPs (they are not ISPs).
    """
    from app.services.iran_operators import operator_label

    now = _utc_now()
    days_i = max(0, min(int(days or 0), 120))
    lim = max(5, min(int(limit or 16), 40))
    since = now - timedelta(days=days_i) if days_i > 0 else None

    hits_q = select(func.count()).select_from(SubFetchEvent)
    users_q = select(func.count(func.distinct(SubFetchEvent.user_id))).select_from(
        SubFetchEvent
    )
    if since is not None:
        hits_q = hits_q.where(SubFetchEvent.ts >= since)
        users_q = users_q.where(SubFetchEvent.ts >= since)

    total_hits = int((await db.execute(hits_q)).scalar_one() or 0)
    total_users = int((await db.execute(users_q)).scalar_one() or 0)

    no_ip_filt = _sub_event_no_public_ip()
    no_ip_hits_q = select(func.count()).select_from(SubFetchEvent).where(no_ip_filt)
    no_ip_users_q = (
        select(func.count(func.distinct(SubFetchEvent.user_id)))
        .select_from(SubFetchEvent)
        .where(no_ip_filt)
    )
    if since is not None:
        no_ip_hits_q = no_ip_hits_q.where(SubFetchEvent.ts >= since)
        no_ip_users_q = no_ip_users_q.where(SubFetchEvent.ts >= since)
    no_public_hits = int((await db.execute(no_ip_hits_q)).scalar_one() or 0)
    no_public_users = int((await db.execute(no_ip_users_q)).scalar_one() or 0)
    public_hits = max(0, total_hits - no_public_hits)

    app_col = func.coalesce(func.nullif(SubFetchEvent.client_app, ""), "نامشخص")
    op_col = func.coalesce(func.nullif(SubFetchEvent.operator, ""), "other")
    brand_col = func.coalesce(func.nullif(SubFetchEvent.device_brand, ""), "نامشخص")

    async def _rank(
        col,
        *,
        relabel=None,
        extra_where=None,
        share_of: int | None = None,
        skip_keys: frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        base = share_of if share_of is not None else total_hits
        q = (
            select(
                col.label("key"),
                func.count().label("hits"),
                func.count(func.distinct(SubFetchEvent.user_id)).label("users"),
                func.max(SubFetchEvent.ts).label("last_ts"),
            )
            .select_from(SubFetchEvent)
        )
        if since is not None:
            q = q.where(SubFetchEvent.ts >= since)
        if extra_where is not None:
            q = q.where(extra_where)
        q = q.group_by(col).order_by(func.count().desc()).limit(lim)
        rows = (await db.execute(q)).all()
        out: list[dict[str, Any]] = []
        skip = skip_keys or frozenset()
        for key, hits, users, last_ts in rows:
            k = str(key or "").strip() or "نامشخص"
            if k in skip:
                continue
            label = relabel(k) if relabel else k
            hits_i = int(hits or 0)
            users_i = int(users or 0)
            out.append(
                {
                    "key": k,
                    "label": label,
                    "hits": hits_i,
                    "users": users_i,
                    "share_pct": (
                        round(hits_i * 100.0 / base, 1) if base else 0.0
                    ),
                    "last_ts": last_ts.isoformat() if last_ts else None,
                }
            )
        return out

    apps = await _rank(app_col)
    operators = await _rank(
        op_col,
        relabel=operator_label,
        extra_where=~no_ip_filt,
        share_of=public_hits,
        skip_keys=frozenset({"other", "unknown", "", "نامشخص"}),
    )
    brands = await _rank(brand_col)

    # Channel split from every distinct client_app (small cardinality)
    ch_q = select(
        app_col.label("key"),
        func.count().label("hits"),
        func.count(func.distinct(SubFetchEvent.user_id)).label("users"),
    ).select_from(SubFetchEvent)
    if since is not None:
        ch_q = ch_q.where(SubFetchEvent.ts >= since)
    ch_q = ch_q.group_by(app_col)
    ch_rows = (await db.execute(ch_q)).all()
    channel_hits = {"app": 0, "browser": 0, "unknown": 0}
    channel_users_n = {"app": 0, "browser": 0, "unknown": 0}
    for key, hits, users in ch_rows:
        app_name = str(key or "").strip() or "نامشخص"
        ch = (
            "app"
            if app_name in _KNOWN_APP_NAMES
            else detect_client_channel("", app_name)
        )
        channel_hits[ch] = channel_hits.get(ch, 0) + int(hits or 0)
        channel_users_n[ch] = channel_users_n.get(ch, 0) + int(users or 0)

    channel_labels = {
        "app": "اپلیکیشن VPN",
        "browser": "مرورگر",
        "unknown": "نامشخص",
    }
    channels: list[dict[str, Any]] = []
    for k in ("app", "browser", "unknown"):
        hits_i = int(channel_hits.get(k, 0))
        if hits_i <= 0:
            continue
        channels.append(
            {
                "key": k,
                "label": channel_labels[k],
                "hits": hits_i,
                "users": int(channel_users_n.get(k, 0)),
                "share_pct": (
                    round(hits_i * 100.0 / total_hits, 1) if total_hits else 0.0
                ),
            }
        )
    channels.sort(key=lambda x: -int(x["hits"]))

    return {
        "generated_at": now.isoformat(),
        "days": days_i,
        "since": since.isoformat() if since else None,
        "total_hits": total_hits,
        "total_users": total_users,
        "public_hits": public_hits,
        "no_public_ip": {
            "hits": no_public_hits,
            "users": no_public_users,
            "share_pct": (
                round(no_public_hits * 100.0 / total_hits, 1) if total_hits else 0.0
            ),
            "label": "بدون IP عمومی (لوکال / پروکسی)",
            "detail": "معمولاً 127.0.0.1 یا unknown — IP واقعی کلاینت به پنل نرسیده",
        },
        "channels": channels,
        "apps": apps,
        "operators": operators,
        "brands": brands,
        "note": (
            "از درخواست‌های واقعی آپدیت/باز شدن لینک ساب · تعداد + کاربران یکتا. "
            "رتبهٔ اپراتور فقط با IP عمومی؛ لوکال/unknown جداست."
        ),
    }


def empty_user_overview(*, tz_offset_min: int = 0, note: str | None = None) -> dict[str, Any]:
    """Minimal valid payload so the Users analytics page never hard-fails."""
    now = _utc_now()
    periods = _period_starts(now, tz_offset_min)
    tz_off = int(periods["tz_offset_min"])
    grid_hour = int(periods["grid_hour"])
    g_start = grid_hour - 23 * 3600
    global_hourly = [
        {
            "hour_ts": g_start + i * 3600,
            "raw_bytes": 0,
            "weighted_bytes": 0,
            "is_current": i == 23,
        }
        for i in range(24)
    ]
    empty_hod = {"bytes": [0] * 24, "peak_hour": None, "peak_bytes": 0}
    return {
        "generated_at": now.isoformat(),
        "tz_offset_min": tz_off,
        "online_users": 0,
        "online_source": "empty",
        "online_updated_at": None,
        "online_panels": [],
        "raw_hour": {
            "current": 0,
            "previous": 0,
            "change_pct": None,
        },
        "raw_today": 0,
        "raw_week": 0,
        "raw_month": 0,
        "raw_current": 0,
        "raw_pending": 0,
        "eligible_users": 0,
        "active_users": {"day": 0, "week": 0, "month": 0, "all": 0},
        "range_stats": {},
        "hour_of_day": empty_hod,
        "global_hourly": global_hourly,
        "global_24h": {
            "raw_bytes": 0,
            "weighted_bytes": 0,
            "source": "empty",
            "note": note or "آمار موقتاً در دسترس نیست — دوباره تلاش کنید",
        },
        "top_hour": [],
        "health_counts": {},
        "health_users": [],
        "note": note
        or "آمار موقتاً سبک بارگذاری شد — چند ثانیه بعد بروزرسانی کنید",
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
        """Sum usage with SQL GROUP BY hour — never pull per-user rows."""
        if end_ts <= start_ts:
            return 0
        lo = _align_down_hour(start_ts)
        hi = _align_up_hour(end_ts)
        rows = (
            await db.execute(
                select(
                    UsageHourBucket.hour_ts,
                    func.coalesce(func.sum(UsageHourBucket.raw_bytes), 0),
                )
                .where(
                    UsageHourBucket.hour_ts >= lo,
                    UsageHourBucket.hour_ts < hi,
                )
                .group_by(UsageHourBucket.hour_ts)
            )
        ).all()
        return sum(
            _overlap_bytes(int(ts), int(raw or 0), start_ts, end_ts)
            for ts, raw in rows
        )

    week = int(periods["week"])
    week_end = int(periods["week_end"])
    month = int(periods["month"])
    next_month_ts = int(periods["next_month"])
    prev_month = int(periods["prev_month"])

    cur = await _sum_all(hour, hour_end)
    prev = await _sum_all(hour - 3600, hour)
    today = await _sum_all(day, day_end)
    yesterday = await _sum_all(day - 86400, day)
    week_bytes = await _sum_all(week, week_end)
    prev_week_bytes = await _sum_all(week - 7 * 86400, week)
    month_bytes = await _sum_all(month, next_month_ts)
    prev_month_bytes = await _sum_all(prev_month, month)

    # Global live = Σ User.used_traffic (weighted, same as users page)
    live_all = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(User.used_traffic), 0))
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
    # Cap absurd pending (pre-rebase scale mismatch) so UI never dumps lifetime
    max_pending = max(500 * 1024 * 1024, int(50_000_000 / 8 * 120))  # ~50MB/s × 2min
    if pending_all > max_pending:
        pending_all = 0
    cur += pending_all
    today += pending_all
    week_bytes += pending_all
    month_bytes += pending_all

    # یک جامعه برای همهٔ این اعداد: فقط status=active (بدون نمونه)
    # تا «صفرمصرف + آنلاین‌شده = کاربران فعال» دقیق دربیاید.
    def _not_sample_clause():
        return (
            ~User.username.startswith("ag"),
            ~func.coalesce(User.note, "").like("%نمونه کانفیگ%"),
        )

    eligible_uids = {
        int(uid)
        for (uid,) in (
            await db.execute(
                select(User.id).where(
                    User.status == UserStatus.active,
                    *_not_sample_clause(),
                )
            )
        ).all()
        if uid is not None
    }
    active_status_n = len(eligible_uids)

    pending_by_uid: dict[int, int] = {}
    if eligible_uids:
        for uid, curs, live in (
            await db.execute(
                select(
                    UserUsageState.user_id,
                    UserUsageState.last_raw_total,
                    User.used_traffic,
                )
                .join(User, User.id == UserUsageState.user_id)
                .where(
                    UserUsageState.last_sample_at.is_not(None),
                    User.status == UserStatus.active,
                    *_not_sample_clause(),
                )
            )
        ).all():
            pend = _pending_bytes(
                live=int(live or 0),
                cursor=int(curs or 0),
                seeded=True,
            )
            # Same cap as low-usage so «صفر» و «آنلاین‌شده» overlap نکنند
            if 0 < pend <= _LOW_USAGE_PENDING_CAP:
                pending_by_uid[int(uid)] = pend

    async def _used_users(start_ts: int, end_ts: int) -> int:
        """Active users with real local-period traffic (not «صفر / استفاده نشده»).

        Partition: آنلاین‌شده (≥1KiB) + صفرمصرف(<1KiB) = کاربران فعال.
        Uses SQL SUM per user (fast) — edge-hour proration is negligible here.
        """
        if end_ts <= start_ts or not eligible_uids:
            return 0
        lo = _align_down_hour(start_ts)
        hi = _align_up_hour(end_ts)
        bytes_by: dict[int, int] = {
            int(uid): int(raw or 0)
            for uid, raw in (
                await db.execute(
                    select(
                        UsageHourBucket.user_id,
                        func.coalesce(func.sum(UsageHourBucket.raw_bytes), 0),
                    )
                    .where(
                        UsageHourBucket.user_id.in_(eligible_uids),
                        UsageHourBucket.hour_ts >= lo,
                        UsageHourBucket.hour_ts < hi,
                    )
                    .group_by(UsageHourBucket.user_id)
                )
            ).all()
        }
        if end_ts > int(now.timestamp()) - 1:
            for uid, pend in pending_by_uid.items():
                bytes_by[uid] = int(bytes_by.get(uid, 0)) + int(pend)
        # Users with only pending (no bucket yet) still count
        for uid, pend in pending_by_uid.items():
            if uid not in bytes_by and int(pend) >= _LOW_USAGE_NONE_MAX:
                bytes_by[uid] = int(pend)
        return sum(
            1 for b in bytes_by.values() if int(b) >= _LOW_USAGE_NONE_MAX
        )

    day_end_live = min(day_end, int(now.timestamp()) + 1)
    active_today = await _used_users(day, day_end_live)
    active_week = await _used_users(week, week_end)
    active_month = await _used_users(month, next_month_ts)
    # «آمار کل» → تعداد کاربران فعال (status=active)، نه used_traffic>0
    active_all = active_status_n
    eligible_n = len(eligible_uids)

    # Same canonical online as panel analytics (host-deduped sys_online).
    online_n = 0
    online_source = "local_sample"
    online_panels: list[dict[str, Any]] = []
    online_updated_at: str | None = None
    try:
        from app.services import panel_analytics as panel_analytics_svc

        snap = await panel_analytics_svc.unified_online_snapshot(
            db, with_attrs=False
        )
        if snap.get("available") and int(snap.get("total_online") or 0) >= 0:
            online_n = int(snap.get("total_online") or 0)
            online_source = str(snap.get("source") or "remote_system")
            online_panels = list(snap.get("panels") or [])
            online_updated_at = snap.get("updated_at")
    except Exception:  # noqa: BLE001
        logger.debug("unified online snapshot failed", exc_info=True)
    if online_n <= 0 and not online_panels:
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
        online_source = "local_sample"

    # Hour-of-day heat — aggregated by hour (not per-user). Cap history at 90d
    # so a cold full-table MIN+scan never freezes the page.
    heat_lo = min(day, week, month, grid_hour - 7 * 86400)
    heat_floor = grid_hour - 90 * 86400
    if heat_lo < heat_floor:
        heat_lo = heat_floor
    oldest_ts = heat_lo
    heat_rows = (
        await db.execute(
            select(
                UsageHourBucket.hour_ts,
                func.coalesce(func.sum(UsageHourBucket.raw_bytes), 0),
            )
            .where(UsageHourBucket.hour_ts >= _align_down_hour(heat_lo))
            .group_by(UsageHourBucket.hour_ts)
        )
    ).all()

    def _peak_for(start_ts: int, end_ts: int) -> tuple[list[int], int | None, int]:
        by = [0] * 24
        lo = _align_down_hour(start_ts)
        hi = _align_up_hour(end_ts)
        for ts, raw in heat_rows:
            tsi = int(ts)
            if tsi < lo or tsi >= hi:
                continue
            by[_hod_index(tsi, tz_off)] += _overlap_bytes(
                tsi, int(raw or 0), start_ts, end_ts
            )
        if pending_all and end_ts > int(now.timestamp()) - 1:
            by[_hod_index(grid_hour, tz_off)] += pending_all
        peak = max(range(24), key=lambda h: by[h]) if any(by) else None
        return by, peak, (by[peak] if peak is not None else 0)

    by_hod_day, peak_day, peak_day_bytes = _peak_for(day, day_end)
    by_hod_week, peak_week, peak_week_bytes = _peak_for(week, week_end)
    by_hod_month, peak_month, peak_month_bytes = _peak_for(month, next_month_ts)
    by_hod_all, peak_all, peak_all_bytes = _peak_for(
        oldest_ts, grid_hour + 3600
    )
    # Legacy overview card: prefer today's peak; fall back to 7d pattern
    by_hod_7d, peak_7d, peak_7d_bytes = _peak_for(
        grid_hour - 7 * 86400, grid_hour + 3600
    )
    by_hod = by_hod_day if any(by_hod_day) else by_hod_7d
    peak_hod = peak_day if peak_day is not None else peak_7d
    peak_hod_bytes = (
        peak_day_bytes if peak_day is not None else peak_7d_bytes
    )

    # Top consumers this local hour — SQL SUM per user (2–3 hour buckets max)
    top_lo = _align_down_hour(hour)
    top_hi = _align_up_hour(hour_end)
    top_rows = (
        await db.execute(
            select(
                UsageHourBucket.user_id,
                User.username,
                func.coalesce(func.sum(UsageHourBucket.raw_bytes), 0),
            )
            .join(User, User.id == UsageHourBucket.user_id)
            .where(
                UsageHourBucket.hour_ts >= top_lo,
                UsageHourBucket.hour_ts < top_hi,
            )
            .group_by(UsageHourBucket.user_id, User.username)
            .order_by(func.coalesce(func.sum(UsageHourBucket.raw_bytes), 0).desc())
            .limit(max(limit_users * 3, 60))
        )
    ).all()
    top_acc: dict[int, dict[str, Any]] = {}
    for uid, uname, raw in top_rows:
        part = int(raw or 0)
        if part <= 0:
            continue
        top_acc[int(uid)] = {
            "username": uname,
            "user_id": int(uid),
            "raw_bytes": part,
        }
    # Add modest pending only for users already in the top set (no full-table scan)
    if pending_all > 0 and top_acc:
        top_uids = list(top_acc.keys())
        state_rows = (
            await db.execute(
                select(
                    UserUsageState.user_id,
                    UserUsageState.last_raw_total,
                    User.username,
                    func.coalesce(User.used_traffic, 0),
                )
                .join(User, User.id == UserUsageState.user_id)
                .where(
                    UserUsageState.user_id.in_(top_uids),
                    UserUsageState.last_sample_at.is_not(None),
                )
            )
        ).all()
        for uid, curs, uname, live in state_rows:
            pend = _pending_bytes(
                live=int(live or 0),
                cursor=int(curs or 0),
                seeded=True,
            )
            if pend <= 0 or pend > max_pending:
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

    # Canonical 24h series — SAME source as panel analytics (PanelUsageHourBucket).
    # Skip live remote flush on overview GET (that starved the pool / timed out).
    global_hourly: list[dict[str, Any]] = []
    global_24h_raw = 0
    global_24h_weighted = 0
    try:
        from app.services import panel_analytics as panel_analytics_svc

        g24 = await panel_analytics_svc.build_global_hourly_24h(
            db, tz_offset_min=tz_offset_min
        )
        global_hourly = list(g24.get("global_hourly") or [])
        global_24h_raw = int(g24.get("raw_24h") or 0)
        global_24h_weighted = int(g24.get("weighted_24h") or 0)
    except Exception:  # noqa: BLE001
        logger.debug("canonical global hourly failed; falling back", exc_info=True)
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
        for i in range(24):
            ts = g_start + i * 3600
            raw = g_map.get(ts, 0)
            if i == 23:
                raw += pending_all
            global_hourly.append(
                {
                    "hour_ts": ts,
                    "raw_bytes": raw,
                    "weighted_bytes": raw,
                    "is_current": i == 23,
                }
            )
        global_24h_raw = sum(int(h.get("raw_bytes") or 0) for h in global_hourly)
        global_24h_weighted = global_24h_raw

    raw_hour = _hour_over_hour(
        cur,
        prev,
        hour_start=hour,
        now_ts=int(now.timestamp()),
    )

    def _range_block(
        *,
        key: str,
        label: str,
        active: int,
        usage_cur: int,
        usage_prev: int,
        peak_h: int | None,
        peak_b: int,
        hod: list[int],
        hour_cmp: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "key": key,
            "label": label,
            "active_users": int(active),
            "usage": {
                "current": int(usage_cur),
                "previous": int(usage_prev),
                "change_pct": _pct_change(usage_cur, usage_prev),
            },
            "hour": hour_cmp,
            "peak_hour": peak_h,
            "peak_bytes": int(peak_b),
            "hour_of_day": {"bytes": hod, "peak_hour": peak_h, "peak_bytes": int(peak_b)},
        }

    range_stats = {
        "day": _range_block(
            key="day",
            label="امروز",
            active=active_today,
            usage_cur=today,
            usage_prev=yesterday,
            peak_h=peak_day if peak_day is not None else peak_hod,
            peak_b=peak_day_bytes if peak_day is not None else peak_hod_bytes,
            hod=by_hod_day if any(by_hod_day) else by_hod,
            hour_cmp=raw_hour,
        ),
        "week": _range_block(
            key="week",
            label="این هفته",
            active=active_week,
            usage_cur=week_bytes,
            usage_prev=prev_week_bytes,
            peak_h=peak_week,
            peak_b=peak_week_bytes,
            hod=by_hod_week,
        ),
        "month": _range_block(
            key="month",
            label="این ماه",
            active=active_month,
            usage_cur=month_bytes,
            usage_prev=prev_month_bytes,
            peak_h=peak_month,
            peak_b=peak_month_bytes,
            hod=by_hod_month,
        ),
        "all": {
            "key": "all",
            "label": "آمار کل",
            "active_users": int(active_all),
            "eligible_users": int(eligible_n),
            "usage": {
                "current": int(live_all),
                "previous": 0,
                "change_pct": None,
            },
            "hour": None,
            "peak_hour": peak_all if peak_all is not None else peak_hod,
            "peak_bytes": int(
                peak_all_bytes if peak_all is not None else peak_hod_bytes
            ),
            "hour_of_day": {
                "bytes": by_hod_all if any(by_hod_all) else by_hod,
                "peak_hour": peak_all if peak_all is not None else peak_hod,
                "peak_bytes": int(
                    peak_all_bytes if peak_all is not None else peak_hod_bytes
                ),
            },
        },
    }

    return {
        "generated_at": now.isoformat(),
        "tz_offset_min": tz_off,
        "online_users": online_n,
        "online_source": online_source,
        "online_updated_at": online_updated_at,
        "online_panels": online_panels[:12],
        "raw_hour": raw_hour,
        "raw_today": today,
        "raw_week": week_bytes,
        "raw_month": month_bytes,
        "raw_current": live_all,
        "raw_pending": pending_all,
        "eligible_users": int(eligible_n),
        "active_users": {
            "day": active_today,
            "week": active_week,
            "month": active_month,
            "all": active_all,
        },
        "range_stats": range_stats,
        "hour_of_day": {
            "bytes": by_hod,
            "peak_hour": peak_hod,
            "peak_bytes": peak_hod_bytes,
        },
        "global_hourly": global_hourly,
        "global_24h": {
            "raw_bytes": int(global_24h_raw),
            "weighted_bytes": int(global_24h_weighted),
            "source": "panel_hour_buckets",
            "note": (
                "همان منبع آمار پنل‌ها · ۲۴ ساعت غلتان (نه روز تقویمی). "
                "ساعت جاری زنده است؛ ساعت‌های قبل بعد از اتمام ساعت ثابت می‌مانند."
            ),
        },
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
        "note": "مصرف با ضریب · مثل صفحه کاربران · بازه‌ها با زمان محلی",
    }


# --- Low / troubled usage (local day / week / month / all) -------------------
_MB = 1024 * 1024
_LOW_USAGE_NONE_MAX = 1024  # < 1 KiB ≈ unused in period
_LOW_USAGE_TINY_MAX = 5 * _MB  # < 5 MiB
_LOW_USAGE_LOW_MAX = 100 * _MB  # < 100 MiB
_LOW_USAGE_PENDING_CAP = 200 * _MB  # ignore absurd unsampled deltas
_LOW_USAGE_PERIODS = ("day", "week", "month", "all")
_LOW_USAGE_PERIOD_LABEL = {
    "day": "امروز",
    "week": "این هفته",
    "month": "این ماه",
    "all": "کل",
}


def _low_usage_tier(period_bytes: int) -> str | None:
    """Return none|tiny|low, or None if ≥100 MiB in the selected period."""
    n = max(0, int(period_bytes or 0))
    if n < _LOW_USAGE_NONE_MAX:
        return "none"
    if n < _LOW_USAGE_TINY_MAX:
        return "tiny"
    if n < _LOW_USAGE_LOW_MAX:
        return "low"
    return None


def _low_usage_meta(
    tier: str, *, never_used: bool, period: str = "day"
) -> dict[str, str]:
    plabel = _LOW_USAGE_PERIOD_LABEL.get(period, "امروز")
    if tier == "none":
        if never_used:
            return {
                "code": "none",
                "label": "اصلاً استفاده نشده",
                "tone": "danger",
                "hint": "هیچ مصرفی روی اکانت ثبت نشده",
            }
        return {
            "code": "none",
            "label": f"{plabel} صفر",
            "tone": "danger",
            "hint": f"در بازهٔ {plabel} تقریباً هیچ ترافیکی رد نشده",
        }
    if tier == "tiny":
        return {
            "code": "tiny",
            "label": "زیر ۵ مگابایت",
            "tone": "danger",
            "hint": f"مصرف {plabel} خیلی کم است — احتمال اختلال",
        }
    return {
        "code": "low",
        "label": "زیر ۱۰۰ مگابایت",
        "tone": "warn",
        "hint": f"مصرف {plabel} کمتر از ۱۰۰ مگ",
    }


async def build_low_usage_today(
    db: AsyncSession,
    *,
    tz_offset_min: int = 0,
    tier: str = "all",
    period: str = "day",
    offset: int = 0,
    limit: int = 80,
    summary_only: bool = False,
) -> dict[str, Any]:
    """Active users with usage under 100 MiB in the selected local period.

    Same pool as overview «کاربران فعال / آنلاین‌شده» so for ``day``:
    صفرمصرف(none) + آنلاین‌شده(=not none) = کاربران فعال.

    Tiers (mutually exclusive, absolute thresholds in the selected window):
    - none: < 1 KiB
    - tiny: < 5 MiB
    - low:  < 100 MiB

    ``period``: day | week | month | all (lifetime used_traffic).
    """
    now = _utc_now()
    periods = _period_starts(now, tz_offset_min)
    day = int(periods["day"])
    day_end = int(periods["day_end"])
    week = int(periods["week"])
    week_end = int(periods["week_end"])
    month = int(periods["month"])
    next_month = int(periods["next_month"])
    now_ts = int(now.timestamp())
    day_end_live = min(day_end, now_ts + 1)
    week_end_live = min(week_end, now_ts + 1)
    month_end_live = min(next_month, now_ts + 1)
    tz_off = int(periods["tz_offset_min"])
    want = (tier or "all").strip().lower()
    if want not in ("all", "none", "tiny", "low"):
        want = "all"
    period_key = (period or "day").strip().lower()
    if period_key not in _LOW_USAGE_PERIODS:
        period_key = "day"
    period_label = _LOW_USAGE_PERIOD_LABEL[period_key]

    if period_key == "day":
        range_start, range_end = day, day_end_live
    elif period_key == "week":
        range_start, range_end = week, week_end_live
    elif period_key == "month":
        range_start, range_end = month, month_end_live
    else:
        range_start, range_end = 0, now_ts + 1

    # فقط active — یکسان با آمار کل / آنلاین‌شده
    users = list(
        (
            await db.execute(
                select(User).where(
                    User.status == UserStatus.active,
                    ~User.username.startswith("ag"),
                    ~func.coalesce(User.note, "").like("%نمونه کانفیگ%"),
                )
            )
        )
        .scalars()
        .all()
    )
    empty_payload = {
        "generated_at": now.isoformat(),
        "tz_offset_min": tz_off,
        "period": period_key,
        "period_label": period_label,
        "day_start": range_start,
        "day_end": range_end,
        "range_start": range_start,
        "range_end": range_end,
        "thresholds": {
            "none_max": _LOW_USAGE_NONE_MAX,
            "tiny_max": _LOW_USAGE_TINY_MAX,
            "low_max": _LOW_USAGE_LOW_MAX,
        },
        "counts": {"none": 0, "tiny": 0, "low": 0, "all": 0},
        "total": 0,
        "offset": int(offset),
        "limit": int(limit),
        "items": [],
    }
    if not users:
        return empty_payload

    uids = [int(u.id) for u in users]
    uid_set = set(uids)
    usage_map: dict[int, int] = {i: 0 for i in uids}

    if period_key == "all":
        for u in users:
            usage_map[int(u.id)] = max(0, int(u.used_traffic or 0))
    else:
        lo = _align_down_hour(range_start)
        hi = _align_up_hour(range_end)
        if uids and hi > lo:
            for uid, ts, raw in (
                await db.execute(
                    select(
                        UsageHourBucket.user_id,
                        UsageHourBucket.hour_ts,
                        UsageHourBucket.raw_bytes,
                    ).where(
                        UsageHourBucket.user_id.in_(uids),
                        UsageHourBucket.hour_ts >= lo,
                        UsageHourBucket.hour_ts < hi,
                    )
                )
            ).all():
                uid_i = int(uid)
                if uid_i not in uid_set:
                    continue
                usage_map[uid_i] = usage_map.get(uid_i, 0) + _overlap_bytes(
                    int(ts), int(raw or 0), range_start, range_end
                )

    # Unsampled live delta (same pending model as overview, capped)
    state_rows = (
        await db.execute(
            select(
                UserUsageState.user_id,
                UserUsageState.last_raw_total,
                UserUsageState.last_sample_at,
                UserUsageState.last_sub_fetch_at,
                UserUsageState.online_since,
            ).where(UserUsageState.user_id.in_(uids))
        )
    ).all()
    state_by: dict[int, Any] = {}
    for uid, curs, sampled, sub_at, online_since in state_rows:
        state_by[int(uid)] = {
            "cursor": int(curs or 0),
            "seeded": sampled is not None,
            "last_sub_fetch_at": sub_at,
            "online_since": online_since,
        }

    # Pending only for windows that include "now" (day/week/month — not needed for all)
    if period_key != "all":
        for u in users:
            uid = int(u.id)
            st = state_by.get(uid)
            if not st or not st["seeded"]:
                continue
            pend = _pending_bytes(
                live=int(u.used_traffic or 0),
                cursor=int(st["cursor"]),
                seeded=True,
            )
            if 0 < pend <= _LOW_USAGE_PENDING_CAP:
                usage_map[uid] = usage_map.get(uid, 0) + pend

    # Device / network counts (batch)
    device_counts: dict[int, int] = {i: 0 for i in uids}
    network_counts: dict[int, int] = {i: 0 for i in uids}
    if uids and not summary_only:
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

    counts = {"none": 0, "tiny": 0, "low": 0, "all": 0}
    items_all: list[dict[str, Any]] = []
    for u in users:
        uid = int(u.id)
        period_b = int(usage_map.get(uid, 0) or 0)
        tcode = _low_usage_tier(period_b)
        if tcode is None:
            continue
        counts[tcode] += 1
        counts["all"] += 1
        if summary_only:
            continue
        if want != "all" and tcode != want:
            continue
        lifetime = int(u.used_traffic or 0)
        never_used = lifetime < _LOW_USAGE_NONE_MAX
        st = state_by.get(uid) or {}
        meta = _low_usage_meta(tcode, never_used=never_used, period=period_key)
        sub_at = st.get("last_sub_fetch_at")
        items_all.append(
            {
                "user_id": uid,
                "username": u.username,
                "display_name": u.display_name,
                "status": getattr(u.status, "value", u.status),
                "tier": tcode,
                "tier_label": meta["label"],
                "tone": meta["tone"],
                "hint": meta["hint"],
                "today_bytes": period_b,  # legacy field name; period bytes
                "period_bytes": period_b,
                "used_traffic": lifetime,
                "data_limit": int(u.data_limit or 0),
                "expire": int(u.expire or 0) if u.expire else 0,
                "never_used": never_used,
                "online": bool(st.get("online_since")),
                "last_sub_fetch_at": (
                    sub_at.isoformat() if sub_at is not None else None
                ),
                "devices": int(device_counts.get(uid, 0)),
                "networks": int(network_counts.get(uid, 0)),
                "created_at": (
                    u.created_at.isoformat() if u.created_at is not None else None
                ),
            }
        )

    base_meta = {
        "generated_at": now.isoformat(),
        "tz_offset_min": tz_off,
        "period": period_key,
        "period_label": period_label,
        "day_start": range_start,
        "day_end": range_end,
        "range_start": range_start,
        "range_end": range_end,
        "thresholds": {
            "none_max": _LOW_USAGE_NONE_MAX,
            "tiny_max": _LOW_USAGE_TINY_MAX,
            "low_max": _LOW_USAGE_LOW_MAX,
        },
    }

    if summary_only:
        return {
            **base_meta,
            "counts": counts,
            "total": int(counts["all"]),
            "offset": 0,
            "limit": 0,
            "items": [],
        }

    severity = {"none": 0, "tiny": 1, "low": 2}
    items_all.sort(
        key=lambda x: (
            severity.get(str(x["tier"]), 9),
            int(x.get("period_bytes") or x.get("today_bytes") or 0),
            str(x["username"] or ""),
        )
    )
    total = len(items_all)
    off = max(0, int(offset))
    lim = max(1, min(int(limit) or 80, 200))
    page = items_all[off : off + lim]
    return {
        **base_meta,
        "counts": counts,
        "total": total,
        "offset": off,
        "limit": lim,
        "items": page,
    }
