"""Weighted usage must count each (panel_id, remote) once across group rows."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.user import PanelAccountStatus
from app.services.orchestrator import (
    _dedupe_accounts_for_weighted,
    _weighted_total_from_stored,
)


def _acc(
    *,
    id: int,
    panel_id: int,
    remote: str,
    used: int,
    status: PanelAccountStatus = PanelAccountStatus.active,
    coeff: float = 1.0,
    group_id: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=id,
        panel_id=panel_id,
        remote_username=remote,
        remote_id=None,
        used_traffic=used,
        status=status,
        applied_coefficient=coeff,
        coeff_anchor_raw=0,
        coeff_anchor_weighted=0,
        group_id=group_id,
    )


def test_dedupe_five_group_rows_same_remote_once():
    mb = 1024**2
    accounts = [
        _acc(id=i, panel_id=57, remote="mamadammmm", used=5 * mb, group_id=10 + i)
        for i in range(5)
    ]
    deduped = _dedupe_accounts_for_weighted(accounts)  # type: ignore[arg-type]
    live = [
        a
        for a in deduped
        if a.status
        in (PanelAccountStatus.active, PanelAccountStatus.error, PanelAccountStatus.pending)
    ]
    assert len(live) == 1
    total = _weighted_total_from_stored(accounts)  # type: ignore[arg-type]
    assert total == 5 * mb


def test_dedupe_prefers_highest_raw_then_coeff():
    mb = 1024**2
    accounts = [
        _acc(id=1, panel_id=4, remote="u1", used=10 * mb, coeff=1.0, group_id=1),
        _acc(id=2, panel_id=4, remote="u1", used=10 * mb, coeff=2.0, group_id=2),
        _acc(id=3, panel_id=4, remote="u1", used=12 * mb, coeff=1.0, group_id=3),
    ]
    deduped = _dedupe_accounts_for_weighted(accounts)  # type: ignore[arg-type]
    assert len([a for a in deduped if a.status == PanelAccountStatus.active]) == 1
    assert int(deduped[0].used_traffic) == 12 * mb
    total = _weighted_total_from_stored(accounts)  # type: ignore[arg-type]
    assert total == 12 * mb  # winner coeff 1.0


def test_pending_with_usage_counts():
    mb = 1024**2
    accounts = [
        _acc(
            id=1,
            panel_id=4,
            remote="u_pending",
            used=8 * mb,
            status=PanelAccountStatus.pending,
            coeff=0.83,
        )
    ]
    total = _weighted_total_from_stored(accounts)  # type: ignore[arg-type]
    assert total == int(round(8 * mb * 0.83))


def test_different_remotes_still_sum():
    mb = 1024**2
    accounts = [
        _acc(id=1, panel_id=2, remote="a", used=3 * mb, coeff=1.0),
        _acc(id=2, panel_id=2, remote="b", used=2 * mb, coeff=1.0),
        _acc(id=3, panel_id=5, remote="a", used=1 * mb, coeff=2.0),
    ]
    total = _weighted_total_from_stored(accounts)  # type: ignore[arg-type]
    assert total == 3 * mb + 2 * mb + 2 * mb
