"""Chart-hour («این ساعت») must equal bucket + pending for the grid column."""

from app.services.panel_analytics import (
    _align_down_hour,
    _chart_hour_bytes,
    _period_bytes_from_panel,
    _period_starts,
)
from datetime import datetime, timezone


def test_chart_hour_includes_full_pending_not_capped_watermark():
    # Live jumped after Xray sync; cursor lag = 250 MiB; bucket already 80 MiB
    live = 10 * 1024 * 1024 * 1024
    bucket = 80 * 1024 * 1024
    pending = 250 * 1024 * 1024
    h = _chart_hour_bytes(live=live, bucket=bucket, pending=pending)
    assert h == bucket + pending

    # Old helper capped pending and could under-report vs chart column truth
    capped = _period_bytes_from_panel(
        live=live, pending=pending, bucket=bucket, watermark=100_000
    )
    assert capped <= h
    assert h > 100_000


def test_chart_hour_never_exceeds_live():
    assert _chart_hour_bytes(live=1000, bucket=800, pending=500) == 1000
    assert _chart_hour_bytes(live=0, bucket=800, pending=500) == 0


def test_grid_hour_matches_chart_column_for_iran_offset():
    # 2026-11-09 16:20 UTC → Iran local 19:50 → chart column starts 16:00 UTC = 19:30 IR
    now = datetime(2026, 11, 9, 16, 20, tzinfo=timezone.utc)
    # getTimezoneOffset for IR (UTC+3:30) is -210
    periods = _period_starts(now, tz_offset_min=-210)
    grid = int(periods["grid_hour"])
    assert grid == _align_down_hour(int(now.timestamp()))
    assert grid == int(datetime(2026, 11, 9, 16, 0, tzinfo=timezone.utc).timestamp())
