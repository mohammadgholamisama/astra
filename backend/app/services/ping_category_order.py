"""Category-aware ping/speed order: 5 مستقیم → 5 تانل → 5 گیمینگ → rest."""

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
    download_of=None,
    upload_of=None,
    packet_of=None,
) -> list[T]:
    """Sort successful items into category slots then leftover by quality.

    Success = positive ping OR speed OR packet loss < 100%.
    Quality: more devices → lower packet loss → higher download → higher upload → lower ping.
    """
    def _num(call, it: T, default: float = 0.0) -> float:
        if call is None:
            return default
        try:
            return float(call(it) or 0)
        except (TypeError, ValueError):
            return default

    def _loss(it: T) -> float:
        if packet_of is None:
            return 101.0
        try:
            v = float(packet_of(it))
        except (TypeError, ValueError):
            return 101.0
        if v < 0:
            return 101.0
        return v

    ok: list[T] = []
    rest: list[T] = []
    for it in items:
        try:
            avg = float(avg_of(it))
        except (TypeError, ValueError):
            avg = -1.0
        dl = _num(download_of, it)
        ul = _num(upload_of, it)
        loss = _loss(it)
        if avg > 0 or dl > 0 or ul > 0 or (0 <= loss < 100):
            ok.append(it)
        else:
            rest.append(it)

    def quality_key(it: T) -> tuple:
        try:
            avg = float(avg_of(it))
        except (TypeError, ValueError):
            avg = -1.0
        # Missing ping sorts after real pings when speeds tie
        avg_sort = avg if avg > 0 else 1e18
        dev = 1
        if device_of is not None:
            try:
                dev = int(device_of(it) or 1)
            except (TypeError, ValueError):
                dev = 1
        dl = _num(download_of, it)
        ul = _num(upload_of, it)
        loss = _loss(it)
        # Ascending sort: negate devices/speeds so higher wins; loss lower wins
        return (-dev, loss, -dl, -ul, avg_sort)

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
    def _packet(d: dict[str, Any]) -> float:
        if d.get("has_packet_loss") or (
            d.get("packet_loss_pct") is not None
            and float(d.get("packet_loss_pct") or -1) >= 0
        ):
            try:
                return float(d.get("packet_loss_pct") or 0)
            except (TypeError, ValueError):
                return 101.0
        return 101.0

    return arrange_by_category_slots(
        items,
        avg_of=lambda d: d.get("avg_ping", -1),
        label_of=lambda d: d.get("label") or "",
        device_of=lambda d: d.get("device_count") or 1,
        download_of=lambda d: d.get("download_kbps") or 0,
        upload_of=lambda d: d.get("upload_kbps") or 0,
        packet_of=_packet,
    )
