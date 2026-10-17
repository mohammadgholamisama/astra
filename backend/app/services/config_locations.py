"""Config location flags: host:port[+header host] → country flag for sub remarks."""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
from urllib.parse import parse_qs, unquote, urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config_location import ConfigLocation

logger = logging.getLogger(__name__)

_lock = threading.RLock()
# Exact: (server_host, port, match_host) → flag when match_host non-empty
_specific_map: dict[tuple[str, int, str], str] = {}
# Fallback: (server_host, port) → flag when rule has empty match_host
_general_map: dict[tuple[str, int], str] = {}
_loaded = False

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_HOST_LABEL = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
)
_IPV4 = re.compile(
    r"^(?:\d{1,3}\.){3}\d{1,3}$"
)


def flag_from_country_code(code: str) -> str:
    """ISO 3166-1 alpha-2 → regional-indicator flag emoji."""
    c = (code or "").strip().upper()
    if len(c) != 2 or not c.isalpha():
        raise ValueError("کد کشور باید دو حرف انگلیسی باشد (مثل DE)")
    return "".join(chr(0x1F1E6 + ord(ch) - 65) for ch in c)


def normalize_country_code(raw: str) -> str:
    c = (raw or "").strip().upper()
    if len(c) != 2 or not c.isalpha():
        raise ValueError("کد کشور باید دو حرف انگلیسی باشد (مثل DE)")
    return c


def normalize_host(raw: str) -> str:
    """Normalize hostname as it appears in share links (may be a single label)."""
    host = _CTRL.sub("", (raw or "").strip())
    host = host.strip("[]").rstrip(".").lower()
    if not host or len(host) > 253:
        raise ValueError("دامنه نامعتبر است")
    if any(ch.isspace() for ch in host) or "/" in host or ":" in host:
        raise ValueError("دامنه نباید فاصله، / یا پورت داشته باشد")
    if _IPV4.match(host):
        parts = host.split(".")
        if any(not p.isdigit() or int(p) > 255 for p in parts):
            raise ValueError("آدرس IP نامعتبر است")
        return host
    # IPv6 compressed forms
    if host.count(":") >= 2:
        return host
    # Allow single-label hosts (e.g. ``real``) — matches literal link authority
    labels = host.split(".")
    for label in labels:
        if not label or not _HOST_LABEL.match(label):
            raise ValueError("دامنه نامعتبر است")
    return host


def normalize_match_host(raw: str | None) -> str:
    """Optional header/SNI host. Empty string = match domain+port only."""
    text = _CTRL.sub("", str(raw or "")).strip()
    if not text:
        return ""
    return normalize_host(text)


def normalize_label(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = _CTRL.sub("", str(raw)).strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return None
    return text[:64]


def _b64decode(raw: str) -> bytes:
    s = (raw or "").strip().replace("-", "+").replace("_", "/")
    pad = "=" * (-len(s) % 4)
    return base64.b64decode(s + pad, validate=False)


def _parse_host_port(authority: str) -> tuple[str, int] | None:
    auth = (authority or "").strip()
    if not auth:
        return None
    if auth.startswith("["):
        m = re.match(r"^\[([^\]]+)\]:(\d{1,5})$", auth)
        if not m:
            return None
        host, port_s = m.group(1), m.group(2)
    else:
        if ":" not in auth:
            return None
        host, port_s = auth.rsplit(":", 1)
        host = host.strip().strip("[]")
    try:
        port = int(port_s)
    except ValueError:
        return None
    if port < 1 or port > 65535 or not host:
        return None
    return host.lower().rstrip("."), port


def _first_query_host(query: str, *keys: str) -> str:
    if not query:
        return ""
    try:
        qs = parse_qs(query, keep_blank_values=False)
    except Exception:  # noqa: BLE001
        return ""
    for key in keys:
        vals = qs.get(key) or qs.get(key.lower()) or qs.get(key.upper())
        if not vals:
            # case-insensitive key scan
            for qk, qv in qs.items():
                if qk.lower() == key.lower() and qv:
                    vals = qv
                    break
        if vals:
            raw = unquote(str(vals[0] or "")).strip().strip("[]").rstrip(".").lower()
            if raw:
                return raw
    return ""


def extract_link_endpoint(link: str) -> tuple[str, int] | None:
    """Server host:port from any share link (vless/vmess/ss/trojan/hysteria/…)."""
    raw = (link or "").strip()
    if not raw or "://" not in raw:
        return None
    scheme, rest = raw.split("://", 1)
    scheme_l = scheme.lower()
    rest = rest.split("#", 1)[0].strip()
    if not rest:
        return None

    if scheme_l == "vmess":
        try:
            data = json.loads(_b64decode(rest).decode("utf-8", errors="ignore"))
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(data, dict):
            return None
        host = str(data.get("add") or data.get("host") or "").strip()
        port_raw = data.get("port")
        try:
            port = int(port_raw)
        except (TypeError, ValueError):
            return None
        if not host or port < 1 or port > 65535:
            return None
        return host.lower().rstrip(".").strip("[]"), port

    if scheme_l in ("ss", "shadowsocks"):
        body = rest.split("?", 1)[0]
        # SIP002: [base64(method:pass) or method:pass]@host:port
        if "@" in body:
            hostport = body.rsplit("@", 1)[-1]
            # drop path if present
            hostport = hostport.split("/", 1)[0]
            return _parse_host_port(unquote(hostport))
        # Legacy: base64(method:password@host:port)
        try:
            decoded = _b64decode(body).decode("utf-8", errors="ignore").strip()
        except Exception:  # noqa: BLE001
            return None
        if "@" not in decoded:
            return None
        hostport = decoded.rsplit("@", 1)[-1].split("/", 1)[0]
        return _parse_host_port(hostport)

    if scheme_l == "ssr":
        try:
            decoded = _b64decode(rest.split("?", 1)[0]).decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return None
        # host:port:proto:method:obfs:base64pass
        parts = decoded.split(":")
        if len(parts) < 2:
            return None
        try:
            port = int(parts[1])
        except ValueError:
            return None
        host = parts[0].strip().lower().rstrip(".")
        if not host or port < 1 or port > 65535:
            return None
        return host, port

    # Generic URI schemes: vless / trojan / hysteria / hysteria2 / tuic / …
    try:
        parts = urlsplit(f"{scheme_l}://{rest}")
    except Exception:  # noqa: BLE001
        return None
    host = (parts.hostname or "").strip().lower().rstrip(".")
    port = parts.port
    if host and port:
        return host, int(port)
    # Fallback when urlsplit fails on odd userinfo
    auth = parts.netloc or ""
    if "@" in auth:
        auth = auth.rsplit("@", 1)[-1]
    return _parse_host_port(auth)


def extract_link_match_host(link: str) -> str:
    """Optional header/SNI host from share link (host= / sni= / peer= / vmess host)."""
    raw = (link or "").strip()
    if not raw or "://" not in raw:
        return ""
    scheme, rest = raw.split("://", 1)
    scheme_l = scheme.lower()
    rest = rest.split("#", 1)[0].strip()
    if not rest:
        return ""

    if scheme_l == "vmess":
        try:
            data = json.loads(_b64decode(rest).decode("utf-8", errors="ignore"))
        except Exception:  # noqa: BLE001
            return ""
        if not isinstance(data, dict):
            return ""
        # Prefer explicit header host / sni over server address
        for key in ("host", "sni", "peer"):
            val = str(data.get(key) or "").strip().strip("[]").rstrip(".").lower()
            if val:
                return val
        return ""

    # URI query: host → sni → peer (order matches common vless/trojan/hy2)
    if "?" in rest:
        _path, _, query = rest.partition("?")
        return _first_query_host(query, "host", "sni", "peer")
    return ""


def flag_for_link(link: str) -> str | None:
    """Resolve flag: specific (host+port+match_host) wins over general (host+port)."""
    ep = extract_link_endpoint(link)
    if not ep:
        return None
    server_host, port = ep
    link_match = extract_link_match_host(link)
    with _lock:
        if link_match:
            specific = _specific_map.get((server_host, port, link_match))
            if specific:
                return specific
        return _general_map.get((server_host, port))


def apply_cache(rows: list[ConfigLocation]) -> None:
    specific: dict[tuple[str, int, str], str] = {}
    general: dict[tuple[str, int], str] = {}
    for row in rows:
        try:
            host = normalize_host(row.host)
            port = int(row.port)
            match = normalize_match_host(getattr(row, "match_host", None) or "")
            flag = flag_from_country_code(row.country_code)
        except (ValueError, TypeError):
            continue
        if match:
            specific[(host, port, match)] = flag
        else:
            general[(host, port)] = flag
    with _lock:
        global _specific_map, _general_map, _loaded
        _specific_map = specific
        _general_map = general
        _loaded = True


async def refresh_cache(db: AsyncSession) -> int:
    result = await db.execute(select(ConfigLocation))
    rows = list(result.scalars().all())
    apply_cache(rows)
    return len(rows)


def to_out(row: ConfigLocation) -> dict:
    code = normalize_country_code(row.country_code)
    match = normalize_match_host(getattr(row, "match_host", None) or "")
    return {
        "id": row.id,
        "host": row.host,
        "port": int(row.port),
        "match_host": match,
        "country_code": code,
        "flag": flag_from_country_code(code),
        "label": row.label,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
