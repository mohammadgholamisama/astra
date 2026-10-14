"""
Shared Astra traffic quota across remote panels.

Formula (what the end-user / admin sees):
    used_astra = Σ over panels  (raw_i × coefficient_i)

Rules:
  - coefficient defaults to 1; each GroupPanel may set its own
  - even with coefficient=1, usage is the SUM of all panels (not max, not one panel)
  - when used_astra >= data_limit → status=limited and remotes are frozen/disabled
  - remotes may still show leftover bytes; Astra decides when the sale is finished

Coefficient periods (mid-flight change 1→2):
  past raw stays at the old rate via anchors; only new raw uses the new coefficient.
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
    """Astra-facing usage for one panel."""
    c = norm_coeff(coeff)
    r = max(0, int(raw or 0))
    ar = max(0, int(anchor_raw or 0))
    aw = max(0, int(anchor_weighted or 0))
    if r < ar:
        # Remote traffic reset
        return int(round(r * c))
    return aw + int(round((r - ar) * c))


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
    used_weighted_total: int,
    used_raw_on_panel: int,
    coefficient: float,
) -> int:
    """Absolute traffic ceiling (bytes) to push to one remote panel.

    Shared remaining is converted to raw allowance on this panel:
        more_raw = remaining_astra / coefficient
        remote_limit = used_raw + more_raw

    When Astra quota is exhausted → freeze at current raw (no more traffic).
    Unlimited Astra (limit<=0) → 0 (unlimited on remote).
    """
    limit = int(astra_limit or 0)
    if limit <= 0:
        return 0
    coeff = norm_coeff(coefficient)
    used_w = max(0, int(used_weighted_total or 0))
    used_r = max(0, int(used_raw_on_panel or 0))
    rem = max(0, limit - used_w)
    if rem <= 0:
        return max(1, used_r) if used_r > 0 else 1
    more_raw = int(rem / coeff)
    return max(1, used_r + more_raw)


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

    - Always when blocked (limited/expired/disabled) or status just flipped
    - When shrink_shared_caps=True and 2+ panels: push so sibling remaining shrinks
    - Single panel is self-enforcing on the remote absolute limit (limit/coeff)
    """
    if force or status_changed or blocked:
        return True
    if shrink_shared_caps and active_panel_count > 1:
        return True
    _ = coefficients  # reserved for future per-coeff heuristics
    return False
