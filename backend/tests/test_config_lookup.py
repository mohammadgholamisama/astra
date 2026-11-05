"""Config lookup must prefer group.config_tag over host/port / panel-name guesses."""

from app.services.config_lookup import (
    _finalize,
    _norm_key,
    _tag_score,
    parse_astra_remark,
)
from app.schemas.config_lookup import ConfigLookupParsed


def test_parse_astra_remark_with_parens_tag():
    raw = "#1. (تانل گیگی 5تومن(5)) 8.6G | Testnewvgc 🇦🇱"
    info = parse_astra_remark(raw)
    assert info["tag"] == "تانل گیگی 5تومن(5)"
    assert "8.6" in info["remain"].replace(" ", "")
    assert "Testnewvgc" in info["user"]


def test_parse_astra_remark_plain_tag():
    raw = "#2. VIP 2.5G | alice"
    info = parse_astra_remark(raw)
    assert info["tag"] == "VIP"
    assert "2.5" in info["remain"]
    assert info["user"] == "alice"


def test_parse_astra_remark_no_tag():
    raw = "#3. 1G | bob"
    info = parse_astra_remark(raw)
    assert info["tag"] == ""
    assert "1" in info["remain"]
    assert info["user"] == "bob"


def test_parse_bare_tag_paste():
    info = parse_astra_remark("تانل گیگی 5تومن(5)")
    assert info["tag"] == "تانل گیگی 5تومن(5)"


def test_tag_score_prefers_config_tag_not_panelish_name():
    q = "تانل گیگی 5تومن(5)"
    assert (
        _tag_score(
            q,
            group_config_tag="تانل گیگی 5تومن(5)",
            group_name="گروه مستقیم EU",
        )
        == 100
    )
    # Wrong group that merely has «تانل» vibes in the name must score 0 vs exact tag
    wrong = _tag_score(
        q,
        group_config_tag="مستقیم",
        group_name="Hajmi16-Tunnel",
    )
    assert wrong == 0
    assert _norm_key("مستقیم") != _norm_key(q)


def test_finalize_keeps_tag_matched_group_only():
    parsed = ConfigLookupParsed(
        remark="#1. (تانل گیگی 5تومن(5)) 8.6G | Testnewvgc",
        config_tag="تانل گیگی 5تومن(5)",
        host="a.example",
        port=443,
    )
    buckets = {
        (1, 10): {
            "panel_id": 1,
            "panel_name": "Hajmi16-Tunnel",
            "panel_type": "pasarguard",
            "panel_is_active": True,
            "group_id": 10,
            "group_name": "گروه مستقیم",
            "group_config_tag": "مستقیم",
            "order_kind": "direct",
            "match_quality": "exact",
            "source_remark": "something",
            "host": "a.example",
            "port": 443,
            "match_host": "",
            "accounts_count": 5,
            "sample_users": ["u1"],
        },
        (1, 20): {
            "panel_id": 1,
            "panel_name": "Hajmi16-Tunnel",
            "panel_type": "pasarguard",
            "panel_is_active": True,
            "group_id": 20,
            "group_name": "تانل گیگی",
            "group_config_tag": "تانل گیگی 5تومن(5)",
            "order_kind": "tunnel",
            "match_quality": "host_port",
            "source_remark": "sample",
            "host": "a.example",
            "port": 443,
            "match_host": "",
            "accounts_count": 0,
            "sample_users": [],
        },
    }
    out = _finalize(buckets, parsed, links=["vless://x"], query_tag="تانل گیگی 5تومن(5)")
    assert len(out.hits) == 1
    assert out.hits[0].group_id == 20
    assert out.hits[0].group_config_tag == "تانل گیگی 5تومن(5)"
    assert out.hits[0].order_kind == "tunnel"
    assert out.detail and "گروه سازنده" in out.detail
    assert "تانل" in (out.detail or "")


def test_finalize_one_owner_even_when_ten_groups_share_panel():
    """Same panel in many groups must not dump 10 candidates — one owner only."""
    parsed = ConfigLookupParsed(
        remark="#7. (مستقیم(5)) 50G | user",
        config_tag="مستقیم(5)",
        host="cdn.example",
        port=443,
    )
    buckets: dict = {}
    for gid in range(1, 11):
        buckets[(1, gid)] = {
            "panel_id": 1,
            "panel_name": "SharedPanel",
            "panel_type": "astra",
            "panel_is_active": True,
            "group_id": gid,
            "group_name": f"گروه {gid}",
            # Only group 5 carries the exact sub remark tag
            "group_config_tag": "مستقیم(5)" if gid == 5 else f"مستقیم({gid})",
            "order_kind": "direct",
            "match_quality": "exact",
            "source_remark": None,
            "host": "cdn.example",
            "port": 443,
            "match_host": "",
            "accounts_count": 3 if gid == 5 else 0,
            "sample_users": ["u"] if gid == 5 else [],
        }
    out = _finalize(buckets, parsed, links=["vless://x"], query_tag="مستقیم(5)")
    assert len(out.hits) == 1
    assert out.hits[0].group_id == 5
    assert out.hits[0].group_config_tag == "مستقیم(5)"
