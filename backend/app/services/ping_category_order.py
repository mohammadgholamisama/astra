"""Category + usage-cost aware sub order.

Layout (every config participates — ping only affects rank inside a slot):
  1) up to 20 مستقیم — sorted by usage_cost↑ then quality
  2) تانل — ascending usage_cost, up to 35 per cost, within a
     fixed tunnel budget so gaming always fits before leftover
     (budget = SUB_MAX − 20 − 9 = 70 → e.g. گیگ ۵ سپس گیگ ۷)
  3) up to 9 گیمینگ — sorted by usage_cost↑ then quality
  4) leftover by cost then quality (extra of above + other)

Category comes ONLY from group.order_kind set by admin — never from
config remark / label text. Missing ping does NOT eject a config from
its slot (it just sorts after scored peers in the same tier).
"""

from __future__ import annotations

from itertools import groupby
from typing import Any, Callable, TypeVar

T = TypeVar("T")

DIRECT_SLOT = 20
TUNNEL_PER_COST = 35
GAMING_SLOT = 9
# Hard cap for normal user subscription feed (probe/app gets uncapped)
SUB_MAX_CONFIGS = 99
# Tunnel window before gaming so 20 + 70 + 9 = 99
TUNNEL_BUDGET = SUB_MAX_CONFIGS - DIRECT_SLOT - GAMING_SLOT  # 70

ORDER_KINDS = ("direct", "tunnel", "gaming", "other")

# Accept canonical keys + Persian (and a few Latin) aliases from admin UI / legacy
_KIND_ALIASES: dict[str, str] = {
    "direct": "direct",
    "مستقیم": "direct",
    "mostaghim": "direct",
    "tunnel": "tunnel",
    "تانل": "tunnel",
    "تونل": "tunnel",
    "gaming": "gaming",
    "گیمینگ": "gaming",
    "گیم": "gaming",
    "game": "gaming",
    "other": "other",
    "دیگر": "other",
    "سایر": "other",
    "": "other",
}


def normalize_order_kind(raw: str | None) -> str:
    """Map admin value → canonical order_kind. Unknown → other."""
    t = " ".join(str(raw or "").split()).strip().lower()
    if t in _KIND_ALIASES:
        return _KIND_ALIASES[t]
    # tolerate mixed case canonical
    low = t.lower()
    if low in ORDER_KINDS:
        return low
    return "other"


def detect_category(label: str | None) -> str:
    """Backward-compat wrapper — same as normalize_order_kind (no remark guessing)."""
    return normalize_order_kind(label)


def _cost_key(raw: Any) -> int:
    """Ascending cost; missing cost sorts after numbered ones."""
    try:
        if raw is None or str(raw).strip() == "":
            return 10**9
        return int(raw)
    except (TypeError, ValueError):
        return 10**9


def arrange_by_category_slots(
    items: list[T],
    *,
    avg_of: Callable[[T], Any],
    label_of: Callable[[T], Any],
    device_of: Callable[[T], Any] | None = None,
    download_of: Callable[[T], Any] | None = None,
    upload_of: Callable[[T], Any] | None = None,
    packet_of: Callable[[T], Any] | None = None,
    jitter_of: Callable[[T], Any] | None = None,
    stability_of: Callable[[T], Any] | None = None,
    category_of: Callable[[T], Any] | None = None,
    cost_of: Callable[[T], Any] | None = None,
    direct_slot: int = DIRECT_SLOT,
    tunnel_per_cost: int = TUNNEL_PER_COST,
    gaming_slot: int = GAMING_SLOT,
    tunnel_budget: int | None = TUNNEL_BUDGET,
) -> list[T]:
    """Sort items into category/cost slots then leftover by quality.

    ``category_of`` must return group.order_kind (or alias). Labels are ignored.
    Every item enters its order_kind bucket — no separate «unscored» exile.
    """

    def _num(call: Callable[[T], Any] | None, it: T, default: float = 0.0) -> float:
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

    def _cat(it: T) -> str:
        if category_of is None:
            return "other"
        try:
            raw = category_of(it)
        except Exception:  # noqa: BLE001
            raw = None
        return normalize_order_kind(str(raw or ""))

    def _cost(it: T) -> int:
        if cost_of is None:
            return 10**9
        try:
            return _cost_key(cost_of(it))
        except Exception:  # noqa: BLE001
            return 10**9

    def quality_key(it: T) -> tuple:
        try:
            avg = float(avg_of(it))
        except (TypeError, ValueError):
            avg = -1.0
        # Scored first; missing/failed ping sorts last inside the same cost tier
        has_score = 0 if avg > 0 else 1
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
        stab = _num(stability_of, it, default=-1.0)
        has_stab = 0 if stab >= 0 else 1
        stab_key = -stab if stab >= 0 else 0.0
        jit = _num(jitter_of, it, default=-1.0)
        jit_key = jit if jit >= 0 else 1e18
        has_metric = 0 if (dl > 0 or ul > 0 or (0 <= loss < 100) or stab >= 0) else 1
        # Stability → jitter → ping → upload → download → packet loss → devices
        # (packet last: tunnels/IR nets often share loss; UL matters more than DL)
        return (
            has_score,
            has_metric,
            has_stab,
            stab_key,
            jit_key,
            avg_sort,
            -ul,
            -dl,
            loss,
            -dev,
        )

    def segment_key(it: T) -> tuple:
        return (_cost(it),) + quality_key(it)

    buckets: dict[str, list[T]] = {
        "direct": [],
        "tunnel": [],
        "gaming": [],
        "other": [],
    }
    for it in items:
        buckets[_cat(it)].append(it)
    for cat in buckets:
        buckets[cat].sort(key=segment_key)

    picked: set[int] = set()
    head: list[T] = []

    # 1) مستقیم — up to N, cheapest cost first then fastest
    for it in buckets["direct"][: max(0, int(direct_slot))]:
        head.append(it)
        picked.add(id(it))

    # 2) تانل — each usage_cost tier ≤ tunnel_per_cost, total ≤ tunnel_budget
    budget = (
        10**9
        if tunnel_budget is None
        else max(0, int(tunnel_budget))
    )
    tunnel_taken = 0
    tunnel = buckets["tunnel"]
    for _cost_val, group in groupby(tunnel, key=_cost):
        if tunnel_taken >= budget:
            break
        tier = list(group)
        taken = 0
        for it in tier:
            if tunnel_taken >= budget:
                break
            if taken >= int(tunnel_per_cost):
                break
            if id(it) in picked:
                continue
            head.append(it)
            picked.add(id(it))
            taken += 1
            tunnel_taken += 1

    # 3) گیمینگ — up to N (always before leftover / extra tunnel tiers)
    for it in buckets["gaming"][: max(0, int(gaming_slot))]:
        if id(it) in picked:
            continue
        head.append(it)
        picked.add(id(it))

    leftover = [it for it in items if id(it) not in picked]
    leftover.sort(key=segment_key)
    return head + leftover


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

    def _jitter(d: dict[str, Any]) -> float:
        try:
            v = float(d.get("jitter_ms")) if d.get("jitter_ms") is not None else -1.0
        except (TypeError, ValueError):
            return -1.0
        return v

    def _stability(d: dict[str, Any]) -> float:
        try:
            v = (
                float(d.get("stability_pct"))
                if d.get("stability_pct") is not None
                else -1.0
            )
        except (TypeError, ValueError):
            return -1.0
        return v

    return arrange_by_category_slots(
        items,
        avg_of=lambda d: d.get("avg_ping", -1),
        label_of=lambda d: d.get("label") or "",
        device_of=lambda d: d.get("device_count") or 1,
        download_of=lambda d: d.get("download_kbps") or 0,
        upload_of=lambda d: d.get("upload_kbps") or 0,
        packet_of=_packet,
        jitter_of=_jitter,
        stability_of=_stability,
        category_of=lambda d: d.get("order_kind")
        or d.get("category_name")
        or d.get("category")
        or "",
        cost_of=lambda d: d.get("usage_cost"),
    )


def arrange_subscription_rows(
    rows: list[dict[str, Any]],
    *,
    ping_rank_of: Callable[[dict[str, Any]], Any] | None = None,
) -> list[dict[str, Any]]:
    """Order one user's sub feed by group.order_kind slots.

    Layout is fixed whether or not AstraPing has scores:
      20 direct → tunnel (≤35/cost, budget 70) → 9 gaming → leftover

    ``ping_rank_of`` only ranks *inside* a slot (lower positive = better).
    Missing / non-positive rank → still stays in its order_kind slot.
    """

    def _kind(row: dict[str, Any]) -> str:
        return normalize_order_kind(
            str(row.get("order_kind") or row.get("category_name") or "")
        )

    def _avg(row: dict[str, Any]) -> float:
        if ping_rank_of is None:
            return -1.0
        try:
            v = float(ping_rank_of(row))
        except (TypeError, ValueError):
            return -1.0
        # Non-positive / huge sentinel = unscored (slot still filled)
        if v <= 0 or v >= 10**6:
            return -1.0
        return v

    # Stamp canonical order_kind so remarks / debug see the same bucket
    for row in rows:
        row["order_kind"] = _kind(row)

    return arrange_by_category_slots(
        rows,
        avg_of=_avg,
        label_of=lambda row: str(row.get("config_tag") or ""),
        category_of=_kind,
        cost_of=lambda row: row.get("usage_cost"),
    )
