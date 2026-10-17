"""Unit tests for global host:port[+header] subscription hide rules."""

from app.models.group_config_hide import GroupConfigHide
from app.services import config_hides as hides
from app.services.config_locations import extract_link_endpoint, extract_link_match_host


def test_extract_vless_endpoint():
    link = "vless://uuid@example.com:8443?encryption=none#tag"
    assert extract_link_endpoint(link) == ("example.com", 8443)


def test_extract_match_host():
    link = (
        "vless://u@c2.example.com:2095?encryption=none"
        "&host=turkiye.example.ir&path=%2F#x"
    )
    assert extract_link_endpoint(link) == ("c2.example.com", 2095)
    assert extract_link_match_host(link) == "turkiye.example.ir"


def test_is_hidden_global_for_all_groups():
    """Hide is global — same endpoint blocked for every subscription."""
    hides.apply_cache(
        [
            GroupConfigHide(
                id=1, group_id=10, host="example.com", port=8443, match_host=""
            ),
        ]
    )
    link = "vless://u@example.com:8443?encryption=none#a"
    assert hides.is_hidden(link) is True
    assert hides.is_hidden(link, group_id=10) is True
    assert hides.is_hidden(link, group_id=99) is True  # other group still blocked
    assert hides.is_hidden("vless://u@example.com:443?encryption=none#c") is False


def test_is_hidden_distinguishes_match_host():
    hides.apply_cache(
        [
            GroupConfigHide(
                id=1,
                group_id=1,
                host="c2.example.com",
                port=2095,
                match_host="turkiye.example.ir",
            ),
            GroupConfigHide(
                id=2,
                group_id=1,
                host="c2.example.com",
                port=2095,
                match_host="germany.example.ir",
            ),
        ]
    )
    link_tr = (
        "vless://u@c2.example.com:2095?encryption=none"
        "&host=turkiye.example.ir#tr"
    )
    link_de = (
        "vless://u@c2.example.com:2095?encryption=none"
        "&host=germany.example.ir#de"
    )
    link_plain = "vless://u@c2.example.com:2095?encryption=none#plain"

    assert hides.is_hidden(link_tr) is True
    assert hides.is_hidden(link_de) is True
    assert hides.is_hidden(link_plain) is False


def test_apply_cache_replaces():
    hides.apply_cache(
        [GroupConfigHide(id=1, group_id=1, host="a.test", port=100, match_host="")]
    )
    assert hides.is_hidden("trojan://x@a.test:100#x") is True
    hides.apply_cache([])
    assert hides.is_hidden("trojan://x@a.test:100#x") is False
