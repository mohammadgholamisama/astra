"""Slot layout: 20 direct → tunnel cost tiers ×35 (budget 70) → 9 gaming."""

from app.services.ping_category_order import (
    SUB_MAX_CONFIGS,
    TUNNEL_BUDGET,
    arrange_by_category_slots,
    detect_category,
)


def _row(label: str, ping: float, *, cat: str = "", cost=None, dl: float = 0):
    return {
        "label": label,
        "category_name": cat,
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
        category_of=lambda d: d.get("category_name") or "",
        cost_of=lambda d: d.get("usage_cost"),
        device_of=lambda d: d.get("device_count") or 1,
        download_of=lambda d: d.get("download_kbps") or 0,
        upload_of=lambda d: d.get("upload_kbps") or 0,
        packet_of=lambda d: d.get("packet_loss_pct")
        if d.get("packet_loss_pct") is not None
        else 101,
    )


def test_detect_category():
    assert detect_category("مستقیم ایران") == "direct"
    assert detect_category("Hajmi-Tunnel") == "tunnel"
    assert detect_category("گیمینگ EU") == "gaming"


def test_layout_slots_and_cost_tiers():
    items = []
    # 25 direct cost 5 (ping 10..34) and 5 direct cost 7
    for i in range(25):
        items.append(_row(f"d5-{i}", 10 + i, cat="مستقیم", cost=5))
    for i in range(5):
        items.append(_row(f"d7-{i}", 5 + i, cat="مستقیم", cost=7))
    # tunnel cost 5: 40, cost 7: 40
    for i in range(40):
        items.append(_row(f"t5-{i}", 20 + i, cat="تانل", cost=5))
    for i in range(40):
        items.append(_row(f"t7-{i}", 15 + i, cat="تانل", cost=7))
    # gaming 12
    for i in range(12):
        items.append(_row(f"g-{i}", 8 + i, cat="گیمینگ", cost=5))

    out = _arrange(items)

    # First 20: all direct, cost 5 before cost 7
    head20 = out[:20]
    assert all(detect_category(x["category_name"]) == "direct" for x in head20)
    assert all(x["usage_cost"] == 5 for x in head20)
    # best pings first within cost 5
    assert head20[0]["avg_ping"] <= head20[1]["avg_ping"]

    # Next 35: tunnel cost 5
    t5 = out[20:55]
    assert len(t5) == 35
    assert all(
        x["usage_cost"] == 5 and detect_category(x["category_name"]) == "tunnel"
        for x in t5
    )

    # Next 35: tunnel cost 7
    t7 = out[55:90]
    assert len(t7) == 35
    assert all(
        x["usage_cost"] == 7 and detect_category(x["category_name"]) == "tunnel"
        for x in t7
    )

    # Next 9: gaming — always before leftover
    g = out[90:99]
    assert len(g) == 9
    assert all(detect_category(x["category_name"]) == "gaming" for x in g)

    capped = out[:SUB_MAX_CONFIGS]
    assert len(capped) == SUB_MAX_CONFIGS
    assert SUB_MAX_CONFIGS == 99
    assert TUNNEL_BUDGET == 70


def test_extra_tunnel_cost_does_not_push_gaming():
    """Third tunnel cost tier must not steal gaming slots inside the 99 head."""
    items = []
    for i in range(20):
        items.append(_row(f"d-{i}", 10 + i, cat="مستقیم", cost=5))
    for i in range(40):
        items.append(_row(f"t5-{i}", 20 + i, cat="تانل", cost=5))
    for i in range(40):
        items.append(_row(f"t7-{i}", 30 + i, cat="تانل", cost=7))
    # Extra tunnel cost without budget room before gaming
    for i in range(40):
        items.append(_row(f"t9-{i}", 40 + i, cat="تانل", cost=9))
    for i in range(12):
        items.append(_row(f"g-{i}", 5 + i, cat="گیمینگ", cost=5))

    out = _arrange(items)
    head = out[:SUB_MAX_CONFIGS]
    assert all(detect_category(x["category_name"]) == "direct" for x in head[:20])
    assert all(x["usage_cost"] == 5 for x in head[20:55])
    assert all(x["usage_cost"] == 7 for x in head[55:90])
    assert all(detect_category(x["category_name"]) == "gaming" for x in head[90:99])
    # cost-9 tunnels live in leftover after the 99 priority head
    assert any(x["usage_cost"] == 9 for x in out[99:])
