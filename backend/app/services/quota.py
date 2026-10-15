"""
Shared Astra traffic quota across remote panels.

Create / remote caps:
    Every panel gets the FULL purchased data_limit (e.g. 4GB → 4GB on all).
    Coefficient does NOT shrink the remote create/modify data_limit.

Usage (what the end-user / admin sees):
    used_astra = Σ over panels  (raw_i × coefficient_i)

Example: panel A raw 30MB × coeff 3 = 90MB; panel B raw 20MB × coeff 2 = 40MB
→ used_astra = 130MB.

When used_astra >= data_limit → status=limited and ALL remotes are frozen/disabled.
Remotes may still show leftover raw bytes; Astra is the source of truth.

Coefficient always applies to the full current raw on that panel (no legacy
period lock). Changing coeff 1→3 immediately recounts existing raw × 3.
"""

from __future__ import annotations


def norm_coeff(raw: float | None, default: float = 1.0) -> float:
    try:
        c = float(raw if raw is not None else default)
    except (TypeError, ValueError):
        c = default
    if c < 0.01:
        c = 0.01
    return c


def weighted_bytes(
    raw: int,
    coeff: float,
    *,
    anchor_raw: int = 0,
    anchor_weighted: int = 0,
) -> int:
    """Astra-facing usage for one panel: raw × current coefficient.

    anchor_* kept for call-site compatibility but ignored — admin expectation is
    always full raw × live group coefficient.
    """
    _ = (anchor_raw, anchor_weighted)
    c = norm_coeff(coeff)
    r = max(0, int(raw or 0))
    return int(round(r * c))


def sum_weighted(parts: list[tuple[int, float, int, int]]) -> int:
    """Sum weighted usage. Each item: (raw, coeff, anchor_raw, anchor_weighted)."""
    total = 0
    for raw, coeff, anchor_raw, anchor_weighted in parts:
        total += weighted_bytes(
            raw, coeff, anchor_raw=anchor_raw, anchor_weighted=anchor_weighted
        )
    return total


def is_quota_exhausted(used: int, limit: int) -> bool:
    lim = int(limit or 0)
    if lim <= 0:
        return False  # unlimited
    return int(used or 0) >= lim


def remaining_weighted(used: int, limit: int) -> int:
    lim = int(limit or 0)
    if lim <= 0:
        return 0  # unlimited → caller treats specially
    return max(0, lim - max(0, int(used or 0)))


def remote_data_limit(
    astra_limit: int,
    *,
    used_weighted_total: int = 0,
    used_raw_on_panel: int = 0,
    coefficient: float = 1.0,
) -> int:
    """Traffic ceiling (bytes) pushed to one remote panel while user is active.

    Always the full purchased Astra quota — coefficient is for usage accounting
    only, not for shrinking remote caps. Exhaustion is handled by freezing
    remotes (status=limited) in build_user_payload / sync_modify.

    Unlimited Astra (limit<=0) → 0 (unlimited on remote).
    """
    limit = int(astra_limit or 0)
    if limit <= 0:
        return 0
    # Keep unused kwargs for call-site compatibility
    _ = (used_weighted_total, used_raw_on_panel, coefficient)
    return limit


def needs_remote_cap_sync(
    *,
    status_changed: bool,
    blocked: bool,
    force: bool,
    active_panel_count: int,
    coefficients: list[float],
    shrink_shared_caps: bool = False,
) -> bool:
    """Whether remotes must receive an updated data_limit / status push.

    Push when blocked (limited/expired/disabled), status flipped, or forced.
    Do NOT shrink remote data_limit mid-flight for coefficients — remotes keep
    the full purchase; Astra disables all panels when weighted usage hits limit.
    """
    if force or status_changed or blocked:
        return True
    _ = (active_panel_count, coefficients, shrink_shared_caps)
    return False
