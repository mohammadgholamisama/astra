"""Per-user sub must arrange by order_kind — not compress global leftover directs."""

from app.services.ping_category_order import (
    DIRECT_SLOT,
    GAMING_SLOT,
    SUB_MAX_CONFIGS,
    arrange_by_category_slots,
)


def _row(kind: str, idx: int, cost: int | None = 5) -> dict:
    return {
        "link": f"vless://uuid-{kind}-{idx}@h{idx}.example:443?security=tls#{kind}{idx}",
        "order_kind": kind,
        "usage_cost": cost,
        "config_tag": kind,
        "panel_id": 1,
    }


def test_user_subset_does_not_leak_direct_past_slot_20():
    """Simulate a user missing most global tunnels: leftover directs must not
    appear until after this user's tunnel + gaming windows."""
    rows: list[dict] = []
    for i in range(20):
        rows.append(_row("direct", i, cost=2))
    for i in range(5):
        rows.append(_row("tunnel", 100 + i, cost=5))
    for i in range(40):
        rows.append(_row("direct", 200 + i, cost=8))  # leftover directs
    for i in range(10):
        rows.append(_row("tunnel", 300 + i, cost=7))
    for i in range(9):
        rows.append(_row("gaming", 400 + i, cost=3))

    # Fake global index: lower = better (same direction as link_order_index)
    index = {r["link"]: n for n, r in enumerate(rows)}

    out = arrange_by_category_slots(
        rows,
        avg_of=lambda r: float(index[r["link"]] + 1),
        label_of=lambda r: r["config_tag"],
        category_of=lambda r: r["order_kind"],
        cost_of=lambda r: r["usage_cost"],
    )
    head = out[:SUB_MAX_CONFIGS]
    assert all(r["order_kind"] == "direct" for r in head[:DIRECT_SLOT])

    tunnels_available = 15
    gaming_available = GAMING_SLOT
    window_end = DIRECT_SLOT + tunnels_available + gaming_available
    assert all(
        r["order_kind"] != "direct" for r in head[DIRECT_SLOT:window_end]
    ), [
        (i + 1, r["order_kind"])
        for i, r in enumerate(head)
        if DIRECT_SLOT <= i < window_end and r["order_kind"] == "direct"
    ]
    assert [r["order_kind"] for r in head[DIRECT_SLOT:window_end]] == (
        ["tunnel"] * tunnels_available + ["gaming"] * gaming_available
    )
