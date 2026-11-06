"""Status/promo must not eat the 20-direct opening of the sub feed."""

from app.services.ping_category_order import DIRECT_SLOT, normalize_order_kind


def test_promo_inserted_after_direct_block_not_before():
    """Mirror collect_links_detailed assembly: status → directs → promo → rest."""
    status = {"kind": "status", "link": "vless://status"}
    promos = [
        {"kind": "promo", "link": "vless://promo1"},
        {"kind": "promo", "link": "vless://promo2"},
    ]
    renamed = (
        [{"order_kind": "direct", "link": f"d{i}"} for i in range(20)]
        + [{"order_kind": "tunnel", "link": f"t{i}"} for i in range(5)]
        + [{"order_kind": "gaming", "link": f"g{i}"} for i in range(3)]
    )

    direct_end = 0
    for idx, row in enumerate(renamed):
        if normalize_order_kind(str(row.get("order_kind") or "")) != "direct":
            break
        direct_end = idx + 1
        if direct_end >= int(DIRECT_SLOT):
            break

    feed = [status] + renamed[:direct_end] + promos + renamed[direct_end:]

    assert feed[0]["kind"] == "status"
    # Next 20 are directs — promos do not sit in this window
    assert all(r.get("order_kind") == "direct" for r in feed[1:21])
    assert feed[21]["kind"] == "promo"
    assert feed[22]["kind"] == "promo"
    assert feed[23]["order_kind"] == "tunnel"
