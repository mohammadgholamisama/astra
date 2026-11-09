"""Chart-hour («این ساعت») must grow with live — not freeze on stale buckets."""

from datetime import datetime, timezone

from app.services.panel_analytics import (
    _align_down_hour,
    _chart_hour_bytes,
    _period_bytes_from_panel,
    _period_from_live_mark,
    _period_starts,
)


def test_chart_hour_includes_full_pending_not_capped_watermark():
    live = 10 * 1024 * 1024 * 1024
    bucket = 80 * 1024 * 1024
    pending = 250 * 1024 * 1024
    h = _chart_hour_bytes(live=live, bucket=bucket, pending=pending)
    assert h == bucket + pending

    capped = _period_bytes_from_panel(
        live=live, pending=pending, bucket=bucket, watermark=100_000
    )
    assert capped <= h
    assert h > 100_000


def test_chart_hour_never_exceeds_live():
    assert _chart_hour_bytes(live=1000, bucket=800, pending=500) == 1000
    assert _chart_hour_bytes(live=0, bucket=800, pending=500) == 0


def test_grid_hour_matches_chart_column_for_iran_offset():
    now = datetime(2026, 11, 9, 16, 20, tzinfo=timezone.utc)
    periods = _period_starts(now, tz_offset_min=-210)
    grid = int(periods["grid_hour"])
    assert grid == _align_down_hour(int(now.timestamp()))
    assert grid == int(datetime(2026, 11, 9, 16, 0, tzinfo=timezone.utc).timestamp())


def test_live_mark_hour_grows_without_flush():
    """After seeding, each live bump must raise «این ساعت» immediately."""
    hour_ts = 1_000_000
    live0 = 50 * 1024**3
    bucket = 10 * 1024**3
    h0, mark_ts, mark_raw = _period_from_live_mark(
        live0,
        mark_ts=0,
        mark_raw=0,
        period_ts=hour_ts,
        bucket=bucket,
    )
    assert h0 == bucket
    assert mark_ts == hour_ts
    assert mark_raw == live0 - bucket

    live1 = live0 + 500 * 1024**2
    h1, mark_ts1, mark_raw1 = _period_from_live_mark(
        live1,
        mark_ts=mark_ts,
        mark_raw=mark_raw,
        period_ts=hour_ts,
        bucket=bucket,
    )
    assert h1 == bucket + 500 * 1024**2
    assert mark_ts1 == hour_ts
    assert mark_raw1 == mark_raw  # baseline stays put
    assert h1 > h0


def test_live_mark_hour_keeps_growing_across_reads():
    hour_ts = 2_000_000
    live = 1000
    h, ts, raw = _period_from_live_mark(
        live, mark_ts=hour_ts, mark_raw=700, period_ts=hour_ts, bucket=200
    )
    assert h == 300
    live2 = 1800
    h2, _, raw2 = _period_from_live_mark(
        live2, mark_ts=ts, mark_raw=raw, period_ts=hour_ts, bucket=200
    )
    assert h2 == 1100
    assert raw2 == raw
