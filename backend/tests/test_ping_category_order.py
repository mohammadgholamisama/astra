"""Slot layout: 20 direct → tunnel cost tiers ×35 (budget 70) → 9 gaming.

Categories come from explicit order_kind only — never from remark labels.
"""

from app.services.ping_category_order import (
    SUB_MAX_CONFIGS,
    TUNNEL_BUDGET,
    arrange_by_category_slots,
    normalize_order_kind,
)


def _row(label: str, ping: float, *, kind: str = "other", cost=None, dl: float = 0):
    return {
        "label": label,
        "order_kind": kind,
        "category_name": kind,
        "usage_cost": cost,
        "avg_ping": ping,
        "download_kbps": dl,
        "upload_kbps": 0,
        "device_count": 1,
        "packet_loss_pct": -1,
    }


def _arrange(items):
    return arrange_by_category_slots(
        items,
        avg_of=lambda d: d["avg_ping"],
        label_of=lambda d: d["label"],
        category_of=lambda d: d.get("order_kind") or d.get("category_name") or "",
        cost_of=lambda d: d.get("usage_cost"),
        device_of=lambda d: d.get("device_count") or 1,
        download_of=lambda d: d.get("download_kbps") or 0,
        upload_of=lambda d: d.get("upload_kbps") or 0,
        packet_of=lambda d: d.get("packet_loss_pct")
        if d.get("packet_loss_pct") is not None
        else 101,
    )


def test_normalize_order_kind():
    assert normalize_order_kind("direct") == "direct"
    assert normalize_order_kind("مستقیم") == "direct"
    assert normalize_order_kind("تانل") == "tunnel"
    assert normalize_order_kind("گیمینگ") == "gaming"
    assert normalize_order_kind("Hajmi-Tunnel") == "other"  # no remark guessing
    assert normalize_order_kind("") == "other"


def test_ignores_label_text():
    """Remark saying تانل must NOT classify without explicit order_kind."""
    items = [
        _row("تانل EU", 10, kind="other", cost=5),
        _row("x", 11, kind="direct", cost=5),
    ]
    out = _arrange(items)
    assert out[0]["order_kind"] == "direct"
    assert out[1]["order_kind"] == "other"


def test_layout_slots_and_cost_tiers():
    items = []
    for i in range(25):
        items.append(_row(f"d5-{i}", 10 + i, kind="direct", cost=5))
    for i in range(5):
        items.append(_row(f"d7-{i}", 5 + i, kind="direct", cost=7))
    for i in range(40):
        items.append(_row(f"t5-{i}", 20 + i, kind="tunnel", cost=5))
    for i in range(40):
        items.append(_row(f"t7-{i}", 15 + i, kind="tunnel", cost=7))
    for i in range(12):
        items.append(_row(f"g-{i}", 8 + i, kind="gaming", cost=5))

    out = _arrange(items)

    head20 = out[:20]
    assert all(x["order_kind"] == "direct" for x in head20)
    assert all(x["usage_cost"] == 5 for x in head20)
    assert head20[0]["avg_ping"] <= head20[1]["avg_ping"]

    t5 = out[20:55]
    assert len(t5) == 35
    assert all(x["usage_cost"] == 5 and x["order_kind"] == "tunnel" for x in t5)

    t7 = out[55:90]
    assert len(t7) == 35
    assert all(x["usage_cost"] == 7 and x["order_kind"] == "tunnel" for x in t7)

    g = out[90:99]
    assert len(g) == 9
    assert all(x["order_kind"] == "gaming" for x in g)

    capped = out[:SUB_MAX_CONFIGS]
    assert len(capped) == SUB_MAX_CONFIGS
    assert SUB_MAX_CONFIGS == 99
    assert TUNNEL_BUDGET == 70


def test_extra_tunnel_cost_does_not_push_gaming():
    items = []
    for i in range(20):
        items.append(_row(f"d-{i}", 10 + i, kind="direct", cost=5))
    for i in range(40):
        items.append(_row(f"t5-{i}", 20 + i, kind="tunnel", cost=5))
    for i in range(40):
        items.append(_row(f"t7-{i}", 30 + i, kind="tunnel", cost=7))
    for i in range(40):
        items.append(_row(f"t9-{i}", 40 + i, kind="tunnel", cost=9))
    for i in range(12):
        items.append(_row(f"g-{i}", 5 + i, kind="gaming", cost=5))

    out = _arrange(items)
    head = out[:SUB_MAX_CONFIGS]
    assert all(x["order_kind"] == "direct" for x in head[:20])
    assert all(x["usage_cost"] == 5 for x in head[20:55])
    assert all(x["usage_cost"] == 7 for x in head[55:90])
    assert all(x["order_kind"] == "gaming" for x in head[90:99])
    assert any(x["usage_cost"] == 9 for x in out[99:])


def test_unscored_still_fills_order_kind_slots():
    """Missing ping must NOT kick configs out of 20/35/9 windows."""
    items = []
    for i in range(20):
        items.append(_row(f"d-{i}", -1, kind="direct", cost=5))  # no ping
    for i in range(35):
        items.append(_row(f"t5-{i}", -1, kind="tunnel", cost=5))
    for i in range(35):
        items.append(_row(f"t7-{i}", -1, kind="tunnel", cost=7))
    for i in range(9):
        items.append(_row(f"g-{i}", -1, kind="gaming", cost=5))
    # one scored other should not jump ahead of unscored directs
    items.append(_row("other-fast", 1, kind="other", cost=1))

    out = _arrange(items)
    head = out[:SUB_MAX_CONFIGS]
    assert all(x["order_kind"] == "direct" for x in head[:20])
    assert all(x["order_kind"] == "tunnel" and x["usage_cost"] == 5 for x in head[20:55])
    assert all(x["order_kind"] == "tunnel" and x["usage_cost"] == 7 for x in head[55:90])
    assert all(x["order_kind"] == "gaming" for x in head[90:99])
    assert out[99]["order_kind"] == "other"
