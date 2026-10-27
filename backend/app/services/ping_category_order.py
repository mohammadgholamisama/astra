"""Category-aware ping order: 5 مستقیم → 5 تانل → 5 گیمینگ → rest by quality."""

from __future__ import annotations

import re
from typing import Any, TypeVar

T = TypeVar("T")

_SLOT = 5

_GAMING = re.compile(r"گیمینگ|gaming|\bgame\b|گیم\b", re.I)
_TUNNEL = re.compile(r"تانل|تونل|tunnel", re.I)
_DIRECT = re.compile(r"مستقیم|mostaghim|direct", re.I)


def detect_category(label: str | None) -> str:
    t = (label or "").lower()
    if _GAMING.search(t):
        return "gaming"
    if _TUNNEL.search(t):
        return "tunnel"
    if _DIRECT.search(t):
        return "direct"
    return "other"


def arrange_by_category_slots(
    items: list[T],
    *,
    avg_of,
    label_of,
    device_of=None,
) -> list[T]:
    """Sort successful items into category slots then leftover by quality.

    ``avg_of`` / ``label_of`` / ``device_of`` are callables: item → value.
    """
    ok: list[T] = []
    rest: list[T] = []
    for it in items:
        try:
            avg = float(avg_of(it))
        except (TypeError, ValueError):
            avg = -1.0
        if avg > 0:
            ok.append(it)
        else:
            rest.append(it)

    def quality_key(it: T) -> tuple[int, float]:
        try:
            avg = float(avg_of(it))
        except (TypeError, ValueError):
            avg = 1e18
        dev = 1
        if device_of is not None:
            try:
                dev = int(device_of(it) or 1)
            except (TypeError, ValueError):
                dev = 1
        # Higher device count first → negate for ascending sort key
        return (-dev, avg)

    buckets: dict[str, list[T]] = {
        "direct": [],
        "tunnel": [],
        "gaming": [],
        "other": [],
    }
    for it in ok:
        buckets[detect_category(str(label_of(it) or ""))].append(it)
    for cat in buckets:
        buckets[cat].sort(key=quality_key)

    picked: set[int] = set()
    head: list[T] = []
    for cat in ("direct", "tunnel", "gaming"):
        for it in buckets[cat][:_SLOT]:
            head.append(it)
            picked.add(id(it))

    leftover = [it for it in ok if id(it) not in picked]
    leftover.sort(key=quality_key)
    return head + leftover + rest


def arrange_rank_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return arrange_by_category_slots(
        items,
        avg_of=lambda d: d.get("avg_ping", -1),
        label_of=lambda d: d.get("label") or "",
        device_of=lambda d: d.get("device_count") or 1,
    )
