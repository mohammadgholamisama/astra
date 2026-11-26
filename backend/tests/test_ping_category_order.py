"""Slot layout: 20 direct → 50 tunnel → 9 gaming → 20 leftover.

Categories come from explicit order_kind only — never from remark labels.
"""

from app.services.ping_category_order import (
    DIRECT_SLOT,
    GAMING_SLOT,
    LEFTOVER_SLOT,
    SUB_MAX_CONFIGS,
    TUNNEL_SLOT,
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


def test_layout_20_50_9_20():
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
    for i in range(30):
        items.append(_row(f"o-{i}", 50 + i, kind="other", cost=9))

    out = _arrange(items)
    assert SUB_MAX_CONFIGS == 99
    assert DIRECT_SLOT == 20 and TUNNEL_SLOT == 50 and GAMING_SLOT == 9
    assert LEFTOVER_SLOT == 20
    assert len(out) == SUB_MAX_CONFIGS

    head20 = out[:20]
    assert all(x["order_kind"] == "direct" for x in head20)
    # cheaper cost first
    assert all(x["usage_cost"] == 5 for x in head20)

    tunnels = out[20:70]
    assert len(tunnels) == 50
    assert all(x["order_kind"] == "tunnel" for x in tunnels)
    # cost 5 fills first, then cost 7
    assert all(x["usage_cost"] == 5 for x in tunnels[:40])
    assert all(x["usage_cost"] == 7 for x in tunnels[40:50])

    g = out[70:79]
    assert len(g) == 9
    assert all(x["order_kind"] == "gaming" for x in g)

    leftover = out[79:99]
    assert len(leftover) == 20


def test_missing_kinds_still_clean():
    """Only 5 direct + tunnels — no crash, no stolen gaming slots."""
    items = []
    for i in range(5):
        items.append(_row(f"d-{i}", 10 + i, kind="direct", cost=5))
    for i in range(80):
        items.append(_row(f"t-{i}", 20 + i, kind="tunnel", cost=5))

    out = _arrange(items)
    assert all(x["order_kind"] == "direct" for x in out[:5])
    assert all(x["order_kind"] == "tunnel" for x in out[5:55])
    # no gaming → leftover takes next 20 tunnels
    assert all(x["order_kind"] == "tunnel" for x in out[55:75])
    assert len(out) == 5 + 50 + 0 + 20


def test_unscored_still_fills_order_kind_slots():
    """Missing ping must NOT kick configs out of 20/50/9 windows."""
    items = []
    for i in range(20):
        items.append(_row(f"d-{i}", -1, kind="direct", cost=5))
    for i in range(50):
        items.append(_row(f"t-{i}", -1, kind="tunnel", cost=5))
    for i in range(9):
        items.append(_row(f"g-{i}", -1, kind="gaming", cost=5))
    items.append(_row("other-fast", 1, kind="other", cost=1))

    out = _arrange(items)
    assert all(x["order_kind"] == "direct" for x in out[:20])
    assert all(x["order_kind"] == "tunnel" for x in out[20:70])
    assert all(x["order_kind"] == "gaming" for x in out[70:79])
    assert out[79]["order_kind"] == "other"
