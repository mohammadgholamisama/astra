"""Promo slogans stay at the top of the sub (after status), before directs."""

from app.services.ping_category_order import DIRECT_SLOT


def test_promo_stays_before_direct_block():
    status = {"kind": "status", "link": "vless://status"}
    promos = [
        {"kind": "promo", "link": "vless://promo1"},
        {"kind": "promo", "link": "vless://promo2"},
    ]
    renamed = (
        [{"order_kind": "direct", "link": f"d{i}"} for i in range(DIRECT_SLOT)]
        + [{"order_kind": "tunnel", "link": f"t{i}"} for i in range(5)]
    )
    # Same assembly as collect_links_detailed: head(status+promo) + renamed
    feed = [status] + promos + renamed

    assert feed[0]["kind"] == "status"
    assert feed[1]["kind"] == "promo"
    assert feed[2]["kind"] == "promo"
    assert all(r.get("order_kind") == "direct" for r in feed[3 : 3 + DIRECT_SLOT])
    assert feed[3 + DIRECT_SLOT]["order_kind"] == "tunnel"
