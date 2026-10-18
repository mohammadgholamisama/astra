"""Host:port[+match_host] extraction + remark flag append."""

from app.services.config_locations import (
    apply_cache,
    extract_link_endpoint,
    extract_link_match_host,
    flag_for_link,
    flag_from_country_code,
    normalize_host,
    normalize_match_host,
)
from app.services.link_remark import apply_display_name, build_config_remark, link_current_remark


class _Loc:
    def __init__(self, host: str, port: int, country_code: str, match_host: str = ""):
        self.host = host
        self.port = port
        self.country_code = country_code
        self.match_host = match_host


def test_flag_from_country_code():
    assert flag_from_country_code("de") == "🇩🇪"
    assert flag_from_country_code("TR") == "🇹🇷"


def test_normalize_host():
    assert normalize_host("SalamSherek.Belzema.IR.") == "salamsherek.belzema.ir"
    assert normalize_host("1.2.3.4") == "1.2.3.4"
    assert normalize_host("real") == "real"
    assert normalize_host("REAL") == "real"
    assert normalize_host("2606:4700:4700::1001") == "2606:4700:4700::1001"
    assert normalize_host("[2a01:4f8:1c16:bad::1]") == "2a01:4f8:1c16:bad::1"
    assert normalize_host("cdn_edge.example.com") == "cdn_edge.example.com"
    assert normalize_host("astra_order_sample") == "astra_order_sample"
    assert normalize_match_host("") == ""
    assert normalize_match_host("  Turkiye.Varzeshfotsal.IR ") == "turkiye.varzeshfotsal.ir"
    assert normalize_match_host(
        "7c7tc7ytcc7tc77tc9y8yctrefuggooh8f7d6e5a"
    ) == "7c7tc7ytcc7tc77tc9y8yctrefuggooh8f7d6e5a"


def test_extract_ss_sip002():
    link = (
        "ss://Y2hhY2hhMjAtaWV0Zi1wb2x5MTMwNTp2N2pKMVpHSXdJSE5oRlpnbGNTMmlR@"
        "salamsherek.belzema.ir:2060#%2329.%20test"
    )
    assert extract_link_endpoint(link) == ("salamsherek.belzema.ir", 2060)
    assert extract_link_match_host(link) == ""


def test_extract_vless_with_host_param():
    link = (
        "vless://76f1756d-0a1b-4de7-a8c6-faa061204328@c2.zynexoz.ir:2095"
        "?encryption=none&security=none&type=httpupgrade"
        "&host=turkiye.varzeshfotsal.ir&path=%2F#%233.%20test"
    )
    assert extract_link_endpoint(link) == ("c2.zynexoz.ir", 2095)
    assert extract_link_match_host(link) == "turkiye.varzeshfotsal.ir"


def test_extract_vless():
    link = (
        "vless://a28eaa40-0990-4df3-a273-5c9ee23704c7@cd.dlc.sazqel.ir:8443"
        "?encryption=none&security=tls&type=ws&path=%2F"
        "&host=tlxsbjqpzx.varzeshfotsal.ir&sni=tlxsbjqpzx.varzeshfotsal.ir"
        "#%2321.%20test"
    )
    assert extract_link_endpoint(link) == ("cd.dlc.sazqel.ir", 8443)
    assert extract_link_match_host(link) == "tlxsbjqpzx.varzeshfotsal.ir"


def test_extract_vmess():
    import base64
    import json

    payload = {
        "v": "2",
        "ps": "x",
        "add": "node.example.com",
        "port": "443",
        "id": "a28eaa40-0990-4df3-a273-5c9ee23704c7",
        "aid": "0",
        "net": "ws",
        "type": "none",
        "host": "cdn.example.com",
        "path": "/",
        "tls": "tls",
    }
    b64 = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    assert extract_link_endpoint(f"vmess://{b64}") == ("node.example.com", 443)
    assert extract_link_match_host(f"vmess://{b64}") == "cdn.example.com"


def test_extract_trojan_and_hysteria():
    assert extract_link_endpoint(
        "trojan://pass@de1.example.net:443?security=tls#x"
    ) == ("de1.example.net", 443)
    assert extract_link_endpoint(
        "hysteria2://secret@nl.example.org:8443?sni=nl.example.org#x"
    ) == ("nl.example.org", 8443)
    assert extract_link_match_host(
        "hysteria2://secret@nl.example.org:8443?sni=nl.example.org#x"
    ) == "nl.example.org"


def test_specific_match_beats_general():
    """Three-field rule wins; general only when specific doesn't match."""
    apply_cache(
        [
            _Loc("c2.zynexoz.ir", 2095, "DE"),  # general domain+port
            _Loc("c2.zynexoz.ir", 2095, "TR", "turkiye.varzeshfotsal.ir"),  # specific
        ]
    )
    with_header = (
        "vless://76f1756d-0a1b-4de7-a8c6-faa061204328@c2.zynexoz.ir:2095"
        "?encryption=none&host=turkiye.varzeshfotsal.ir&path=%2F#x"
    )
    other_header = (
        "vless://76f1756d-0a1b-4de7-a8c6-faa061204328@c2.zynexoz.ir:2095"
        "?encryption=none&host=germany.example.com&path=%2F#x"
    )
    no_header = (
        "vless://76f1756d-0a1b-4de7-a8c6-faa061204328@c2.zynexoz.ir:2095"
        "?encryption=none&path=%2F#x"
    )
    assert flag_for_link(with_header) == "🇹🇷"
    assert flag_for_link(other_header) == "🇩🇪"
    assert flag_for_link(no_header) == "🇩🇪"


def test_specific_only_does_not_apply_without_header():
    apply_cache(
        [_Loc("c2.zynexoz.ir", 2095, "TR", "turkiye.varzeshfotsal.ir")]
    )
    no_header = (
        "vless://uuid@c2.zynexoz.ir:2095?encryption=none&path=%2F#x"
    )
    assert flag_for_link(no_header) is None
    with_header = (
        "vless://uuid@c2.zynexoz.ir:2095"
        "?encryption=none&host=turkiye.varzeshfotsal.ir#x"
    )
    assert flag_for_link(with_header) == "🇹🇷"


def test_remark_appends_flag_after_name():
    apply_cache([_Loc("salamsherek.belzema.ir", 2060, "DE")])
    link = (
        "ss://Y2hhY2hhMjAtaWV0Zi1wb2x5MTMwNTp2N2pKMVpHSXdJSE5oRlpnbGNTMmlR@"
        "salamsherek.belzema.ir:2060#old"
    )
    assert flag_for_link(link) == "🇩🇪"
    rows = apply_display_name(
        [{"link": link, "panel_id": 1}],
        None,
        username="mamaliiii",
        used=0,
        limit=50 * 1024**3,
    )
    remark = rows[0]["remark"]
    assert remark.endswith("| mamaliiii 🇩🇪")
    assert "🇩🇪" in link_current_remark(rows[0]["link"])


def test_build_config_remark_without_flag():
    assert build_config_remark(name="u", config_id=1, limit=0) == "#1. ∞ | u"


def test_no_flag_when_unmatched():
    apply_cache([_Loc("other.example.com", 443, "US")])
    link = "vless://uuid@cd.dlc.sazqel.ir:8443?encryption=none#x"
    assert flag_for_link(link) is None
    rows = apply_display_name(
        [{"link": link, "panel_id": 1}],
        None,
        username="user1",
        used=0,
        limit=0,
    )
    assert rows[0]["remark"].endswith("| user1")
    assert "🇺🇸" not in rows[0]["remark"]
