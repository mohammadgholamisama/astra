"""
Shared Astra traffic quota across remote panels.

Remote caps (create / modify):
    Each remote gets:
        used_raw_on_panel + (remaining_astra_budget / coefficient / active_panel_count)
    Remaining is split across live remotes so simultaneous download cannot
    N× overshoot the sale limit. Near the end, absolute headroom caps shrink
    further (512MiB → 256MiB → 64MiB) so a late meter tick cannot bleed GBs.

Usage (what the end-user / admin sees):
    used_astra = Σ over accounts period-locked weighted bytes:
      weighted = coeff_anchor_weighted + (raw - coeff_anchor_raw) × applied_coefficient
    Ingest path also does users.used_traffic += round(raw_delta × live_coeff)
    so coefficient changes never rewrite historical consumption.

When used_astra >= data_limit → status=limited and ALL remotes are frozen/disabled.
Astra is the source of truth; remotes are tightened as remaining shrinks.
"""

from __future__ import annotations


_DIGIT_TRANS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)

_MiB = 1024 * 1024
# Absolute raw grow allowed per panel as remaining shrinks (after split).
_HEADROOM_3GB = 512 * _MiB
_HEADROOM_1GB = 256 * _MiB
_HEADROOM_256MB = 64 * _MiB


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
    """Period-locked Astra usage for one account.

    Past bytes stay at the coefficient that was live when they were ingested;
    only ``(raw - anchor_raw)`` uses the current ``coeff``.
    """
    c = norm_coeff(coeff)
    r = max(0, int(raw or 0))
    ar = max(0, int(anchor_raw or 0))
    aw = max(0, int(anchor_weighted or 0))
    if ar <= 0 and aw <= 0:
        return int(round(r * c))
    delta = max(0, r - ar)
    return aw + int(round(delta * c))


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
    active_panel_count: int = 1,
) -> int:
    """Traffic ceiling (bytes) pushed to one remote panel while user is active.

    Ceiling = raw already on this panel + share of remaining Astra budget.
    Remaining is divided by ``active_panel_count`` so multi-panel users cannot
    download the full leftover on every panel at once (the classic
    40GB-sale → 44GB+ overshoot). Near the end, absolute headroom caps shrink.

    Unlimited Astra (limit<=0) → 0 (unlimited on remote).
    """
    limit = int(astra_limit or 0)
    if limit <= 0:
        return 0
    used_w = max(0, int(used_weighted_total or 0))
    used_r = max(0, int(used_raw_on_panel or 0))
    c = norm_coeff(coefficient)
    n = max(1, int(active_panel_count or 1))
    remain_w = max(0, limit - used_w)
    if remain_w <= 0:
        # No sale budget left — freeze grow at current raw.
        return max(1, used_r) if used_r > 0 else 1

    remain_raw = int(round(remain_w / c)) if c > 0 else int(remain_w)
    # Split leftover across live remotes (integer share; remainder stays unallocated).
    grow = remain_raw // n

    # Absolute headroom as the sale ends — meter lag must not bleed GBs.
    if remain_w <= 256 * _MiB:
        grow = min(grow, _HEADROOM_256MB)
    elif remain_w <= 1024 * _MiB:
        grow = min(grow, _HEADROOM_1GB)
    elif remain_w <= 3 * 1024 * _MiB:
        grow = min(grow, _HEADROOM_3GB)

    grow = min(grow, remain_raw)
    if grow < 1:
        grow = 1
    return used_r + grow


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
    ratio = float(used_ratio) if used_ratio is not None else 0.0
    # Tighten earlier than before — remotes must shrink before Hot lag bleeds.
    if ratio >= 0.50:
        return True
    # Multi-panel: start splitting pushes even earlier (duplicated headroom risk).
    if int(active_panel_count or 0) >= 2 and ratio >= 0.35:
        return True
    if shrink_shared_caps and ratio >= 0.35:
        return True
    _ = coefficients
    return False
