"""Unit tests for shared Astra traffic quota + coefficients."""

from app.services.quota import (
    is_quota_exhausted,
    needs_remote_cap_sync,
    remote_data_limit,
    sum_weighted,
    weighted_bytes,
)


def test_weighted_simple_coeff_two():
    # 500MB raw × 2 = 1GB Astra
    assert weighted_bytes(500 * 1024**2, 2) == 1000 * 1024**2


def test_sum_across_panels_coeff_one():
    # Even with coeff=1, panels SUM (not max)
    gb = 1024**3
    total = sum_weighted(
        [
            (3 * gb, 1.0, 0, 0),
            (2 * gb, 1.0, 0, 0),
        ]
    )
    assert total == 5 * gb


def test_sum_across_panels_coeff_two():
    gb = 1024**3
    # Panel A 2GB raw ×2 = 4GB; Panel B 1GB raw ×2 = 2GB → 6GB Astra
    total = sum_weighted(
        [
            (2 * gb, 2.0, 0, 0),
            (1 * gb, 2.0, 0, 0),
        ]
    )
    assert total == 6 * gb


def test_quota_exhausted_marks_limited():
    gb = 1024**3
    assert is_quota_exhausted(10 * gb, 10 * gb) is True
    assert is_quota_exhausted(10 * gb - 1, 10 * gb) is False
    assert is_quota_exhausted(100, 0) is False  # unlimited


def test_remote_limit_full_purchase_ignores_coeff():
    gb = 1024**3
    # 4GB purchase + coeff 2 → still 4GB on remote (not 2GB)
    assert (
        remote_data_limit(
            4 * gb, used_weighted_total=0, used_raw_on_panel=0, coefficient=2
        )
        == 4 * gb
    )
    # Mid-usage still full purchase on remote
    assert (
        remote_data_limit(
            10 * gb,
            used_weighted_total=8 * gb,
            used_raw_on_panel=4 * gb,
            coefficient=2,
        )
        == 10 * gb
    )


def test_remote_limit_unlimited():
    assert remote_data_limit(0, used_weighted_total=0, used_raw_on_panel=0, coefficient=5) == 0


def test_coeff_period_keeps_past_at_old_rate():
    gb = 1024**3
    # Locked 4GB at coeff 1, then +1GB raw at coeff 2 → 4 + 2 = 6GB
    assert (
        weighted_bytes(5 * gb, 2, anchor_raw=4 * gb, anchor_weighted=4 * gb)
        == 6 * gb
    )


def test_push_only_when_blocked_or_forced():
    assert needs_remote_cap_sync(
        status_changed=False,
        blocked=True,
        force=False,
        active_panel_count=1,
        coefficients=[1],
    )
    assert needs_remote_cap_sync(
        status_changed=True,
        blocked=False,
        force=False,
        active_panel_count=1,
        coefficients=[1],
    )
    # No mid-flight shrink for multi-panel coefficients
    assert not needs_remote_cap_sync(
        status_changed=False,
        blocked=False,
        force=False,
        active_panel_count=2,
        coefficients=[1, 2],
        shrink_shared_caps=True,
    )
    assert needs_remote_cap_sync(
        status_changed=False,
        blocked=False,
        force=True,
        active_panel_count=2,
        coefficients=[1, 1],
    )
