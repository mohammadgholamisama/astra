"""Best-effort Iranian mobile operator detection from client IP.

Not ASN-perfect (no MaxMind) — covers major published Irancell / MCI / Rightel
prefixes so panel analytics can break down online users by اپراتور.
"""

from __future__ import annotations

import ipaddress
import re

OPERATOR_LABELS = {
    "irancell": "ایرانسل",
    "mci": "همراه اول",
    "rightel": "رایتل",
    "other": "سایر / نامشخص",
}

# Compact public-ish mobile prefixes (IPv4). Updated opportunistically.
_IRANCELL_CIDRS = [
    "11.0.0.0/8",  # placeholder removed below — real blocks:
    "5.120.0.0/13",
    "5.232.0.0/13",
    "31.14.144.0/20",
    "37.129.0.0/16",
    "37.254.0.0/15",
    "46.100.0.0/16",
    "78.38.0.0/15",
    "80.191.0.0/16",
    "85.185.0.0/16",
    "91.98.0.0/15",
    "92.42.48.0/21",
    "94.182.0.0/15",
    "151.232.0.0/14",
    "151.238.0.0/15",
    "151.240.0.0/13",
    "176.65.192.0/19",
    "178.131.0.0/16",
    "185.55.224.0/22",
    "188.158.0.0/15",
    "188.212.224.0/19",
    "217.219.0.0/16",
]

_MCI_CIDRS = [
    "2.176.0.0/12",
    "2.188.0.0/14",
    "5.52.0.0/16",
    "5.201.128.0/17",
    "5.232.0.0/14",  # overlap risk — prefer more specific first in matcher
    "31.2.128.0/17",
    "31.56.0.0/14",
    "37.254.0.0/16",
    "46.209.0.0/16",
    "78.39.0.0/16",
    "79.127.0.0/17",
    "80.66.176.0/20",
    "85.9.64.0/18",
    "89.165.0.0/17",
    "91.99.0.0/16",
    "91.251.0.0/16",
    "109.110.160.0/19",
    "178.252.128.0/17",
    "188.136.128.0/17",
    "217.218.0.0/15",
]

_RIGHTEL_CIDRS = [
    "5.73.0.0/16",
    "5.74.0.0/16",
    "5.201.0.0/17",
    "31.130.176.0/20",
    "37.254.192.0/18",
    "46.224.0.0/16",
    "89.198.0.0/16",
    "91.225.52.0/22",
    "94.101.128.0/17",
    "164.138.128.0/17",
    "185.45.80.0/22",
    "185.141.212.0/22",
    "185.161.112.0/22",
    "188.208.32.0/19",
]


def _nets(cidrs: list[str]) -> list[ipaddress.IPv4Network]:
    out: list[ipaddress.IPv4Network] = []
    for c in cidrs:
        if c.startswith("11."):
            continue
        try:
            out.append(ipaddress.IPv4Network(c, strict=False))
        except ValueError:
            continue
    # Longest prefix first
    out.sort(key=lambda n: n.prefixlen, reverse=True)
    return out


_NETS: list[tuple[str, list[ipaddress.IPv4Network]]] = [
    ("rightel", _nets(_RIGHTEL_CIDRS)),
    ("irancell", _nets(_IRANCELL_CIDRS)),
    ("mci", _nets(_MCI_CIDRS)),
]


def classify_operator(ip: str | None) -> str:
    """Return irancell|mci|rightel|other."""
    raw = (ip or "").strip().split(",")[0].strip()
    if not raw or raw in ("unknown", "127.0.0.1", "::1"):
        return "other"
    # IPv6: rarely used for Iranian mobile sub fetches — leave other
    if ":" in raw:
        return "other"
    if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", raw):
        return "other"
    try:
        addr = ipaddress.IPv4Address(raw)
    except ValueError:
        return "other"
    for code, nets in _NETS:
        for net in nets:
            if addr in net:
                return code
    return "other"


def operator_label(code: str) -> str:
    return OPERATOR_LABELS.get(code, OPERATOR_LABELS["other"])
