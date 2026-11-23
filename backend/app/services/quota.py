"""
Shared Astra traffic quota across remote panels.

Remote caps (create / modify):
    Each remote gets: used_raw_on_panel + remaining_astra_budget / coefficient.
    Example: buy 10GB, Astra used 8GB, coeff 1, raw on panel 3GB
      → remote data_limit = 3GB + 2GB = 5GB (cannot grow past Astra sale).
    This stops multi-panel overshoot where every panel had the FULL 10GB.

Usage (what the end-user / admin sees):
    used_astra = Σ over accounts  (raw_i × coefficient_of_that_account's_group)

When used_astra >= data_limit → status=limited and ALL remotes are frozen/disabled.
Astra is the source of truth; remotes are tightened as remaining shrinks.

Coefficient always applies to the full current raw on that account (no legacy
period lock). Changing coeff 1→3 immediately recounts existing raw × 3.
"""

from __future__ import annotations


_DIGIT_TRANS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)


def norm_coeff(raw: float | str | None, default: float = 1.0) -> float:
    """Normalize Astra usage coefficient (e.g. 1.33 → raw×1.33 on Astra).

    Accepts int/float/str; comma decimals (1,33) and Persian/Arabic digits.
    """
    if isinstance(raw, str):
        s = raw.strip().translate(_DIGIT_TRANS).replace(",", ".")
        raw = s if s else default
    try:
        c = float(raw if raw is not None else default)
    except (TypeError, ValueError):
        c = float(default)
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
    """Astra-facing usage for one account: raw × current coefficient.

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

    Ceiling = raw already on this panel + remaining Astra budget / coeff.
    So remotes self-cut near the sale end instead of each holding the full
    purchase (which let users overshoot across many panels).

    Unlimited Astra (limit<=0) → 0 (unlimited on remote).
    """
    limit = int(astra_limit or 0)
    if limit <= 0:
        return 0
    used_w = max(0, int(used_weighted_total or 0))
    used_r = max(0, int(used_raw_on_panel or 0))
    c = norm_coeff(coefficient)
    remain_w = max(0, limit - used_w)
    grow = int(round(remain_w / c)) if c > 0 else int(remain_w)
    return used_r + max(0, grow)


def needs_remote_cap_sync(
    *,
    status_changed: bool,
    blocked: bool,
    force: bool,
    active_panel_count: int,
    coefficients: list[float],
    shrink_shared_caps: bool = False,
    used_ratio: float | None = None,
) -> bool:
    """Whether remotes must receive an updated data_limit / status push.

    Push when blocked, status flipped, forced, or usage is mid/high so remaining
    caps on remotes stay tight (prevents overshoot before freeze).
    """
    if force or status_changed or blocked:
        return True
    if used_ratio is not None and float(used_ratio) >= 0.35:
        return True
    if shrink_shared_caps and used_ratio is not None and float(used_ratio) >= 0.15:
        return True
    _ = (active_panel_count, coefficients)
    return False
