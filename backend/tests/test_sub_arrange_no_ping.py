"""Sub layout follows group.order_kind even with zero ping scores."""

from app.services.ping_category_order import (
    DIRECT_SLOT,
    GAMING_SLOT,
    SUB_MAX_CONFIGS,
    arrange_subscription_rows,
)


def _row(kind: str, i: int, cost: int | None = 5) -> dict:
    return {
        "link": f"vless://id-{kind}-{i}@h{i}.t:443?security=tls#{kind}{i}",
        "order_kind": kind,
        "usage_cost": cost,
        "config_tag": kind,
        "panel_id": 1,
    }


def test_slots_without_any_ping():
    rows = (
        [_row("direct", i) for i in range(30)]
        + [_row("tunnel", i, cost=5) for i in range(40)]
        + [_row("tunnel", 100 + i, cost=7) for i in range(40)]
        + [_row("gaming", i) for i in range(12)]
        + [_row("other", i) for i in range(5)]
    )
    # No ping ranks at all
    out = arrange_subscription_rows(rows, ping_rank_of=lambda _r: 10**6)
    head = out[:SUB_MAX_CONFIGS]
    assert [r["order_kind"] for r in head[:DIRECT_SLOT]] == ["direct"] * DIRECT_SLOT
    # Next: tunnels only (cost 5 then 7), no direct leakage
    mid = head[DIRECT_SLOT : DIRECT_SLOT + 70]
    assert all(r["order_kind"] == "tunnel" for r in mid)
    assert all(r["usage_cost"] == 5 for r in mid[:35])
    assert all(r["usage_cost"] == 7 for r in mid[35:70])
    g = head[DIRECT_SLOT + 70 : DIRECT_SLOT + 70 + GAMING_SLOT]
    assert [r["order_kind"] for r in g] == ["gaming"] * GAMING_SLOT


def test_persian_alias_normalized():
    rows = [
        {"link": "a", "order_kind": "مستقیم", "usage_cost": 1, "config_tag": "d"},
        {"link": "b", "order_kind": "تانل", "usage_cost": 5, "config_tag": "t"},
        {"link": "c", "order_kind": "گیمینگ", "usage_cost": 1, "config_tag": "g"},
    ]
    out = arrange_subscription_rows(rows)
    assert [r["order_kind"] for r in out] == ["direct", "tunnel", "gaming"]
