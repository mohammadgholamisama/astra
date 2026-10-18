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
_specific_codes: dict[tuple[str, int, str], str] = {}
# Fallback: (server_host, port) → flag when rule has empty match_host
_general_map: dict[tuple[str, int], str] = {}
_general_codes: dict[tuple[str, int], str] = {}
_loaded = False

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
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


# Fingerprint hosts may include underscores / long reality tokens from real share links
_FP_HOST_CHARS = re.compile(r"^[a-z0-9_.*-]+$")


def normalize_host(raw: str) -> str:
    """Normalize share-link authority for order / hide / flag fingerprints.

    Must accept every host ``extract_link_endpoint`` can return — strict DNS
    label rules would reject real configs (IPv6, underscores, odd SNI) and
    break save/reorder on the Config Order page.
    """
    host = _CTRL.sub("", (raw or "").strip())
    host = host.strip("[]").rstrip(".").lower()
    if not host or len(host) > 253:
        raise ValueError("دامنه نامعتبر است")
    if any(ch.isspace() for ch in host) or "/" in host:
        raise ValueError("دامنه نباید فاصله یا / داشته باشد")
    # Mistaken ``host:port`` (single colon + numeric) — not IPv6
    if host.count(":") == 1:
        _left, right = host.rsplit(":", 1)
        if right.isdigit():
            raise ValueError("دامنه نباید پورت داشته باشد")
    if _IPV4.match(host):
        parts = host.split(".")
        if any(not p.isdigit() or int(p) > 255 for p in parts):
            raise ValueError("آدرس IP نامعتبر است")
        return host
    # IPv6 — hex + colons, optional zone id
    if ":" in host:
        bare = host.split("%", 1)[0]
        if bare.count(":") >= 2 and all(c in "0123456789abcdef:" for c in bare):
            return bare
        raise ValueError("آدرس IPv6 نامعتبر است")
    # Hostname / single-label / CDN / reality id — keep as fingerprint key
    if _FP_HOST_CHARS.fullmatch(host):
        return host
    # Last resort: printable ASCII used in odd share links
    if all(33 <= ord(c) <= 126 for c in host) and "@" not in host and "?" not in host:
        return host
    raise ValueError("دامنه نامعتبر است")


def normalize_match_host(raw: str | None) -> str:
    """Optional header/SNI host. Empty string = match domain+port only."""
    text = _CTRL.sub("", str(raw or "")).strip()
    if not text:
        return ""
    try:
        return normalize_host(text)
    except ValueError:
        # Exotic SNI tokens (long reality ids, etc.) — keep as lowercase fingerprint
        host = text.strip("[]").rstrip(".").lower()
        if (
            not host
            or len(host) > 253
            or any(ch.isspace() for ch in host)
            or "/" in host
            or "@" in host
        ):
            raise ValueError("هاست هدر نامعتبر است") from None
        return host


def try_normalize_endpoint(
    host: str, port: int, match_host: str = ""
) -> tuple[str, int, str] | None:
    """Return normalized (host, port, match) or None if fingerprint is unusable."""
    try:
        h = normalize_host(host)
        p = int(port)
        m = normalize_match_host(match_host or "")
    except (ValueError, TypeError):
        return None
    if p < 1 or p > 65535:
        return None
    return h, p, m


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
        # Prefer explicit header host / sni / authority over server address
        for key in ("host", "sni", "peer", "authority"):
            val = str(data.get(key) or "").strip().strip("[]").rstrip(".").lower()
            if val:
                return val
        return ""

    # URI query: host → sni → peer → authority (grpc / reality)
    if "?" in rest:
        _path, _, query = rest.partition("?")
        return _first_query_host(query, "host", "sni", "peer", "authority")
    return ""


def flag_for_link(link: str) -> str | None:
    """Resolve flag: specific (host+port+match_host) wins over general (host+port)."""
    info = resolve_location_for_link(link)
    return info["flag"] if info else None


def resolve_location_for_endpoint(
    host: str, port: int, match_host: str = ""
) -> dict[str, object] | None:
    """Same matching rules as sub remarks — exact (host,port,match) then general (host,port).

    ``exact=True`` means a DB rule exists for this fingerprint (safe to clear).
    ``exact=False`` means the flag is inherited from host:port with empty match_host.
    """
    try:
        h = normalize_host(host)
        p = int(port)
        m = normalize_match_host(match_host or "")
    except (ValueError, TypeError):
        return None
    with _lock:
        if m:
            flag = _specific_map.get((h, p, m))
            code = _specific_codes.get((h, p, m))
            if flag and code:
                return {
                    "host": h,
                    "port": p,
                    "match_host": m,
                    "country_code": code,
                    "flag": flag,
                    "exact": True,
                }
        flag = _general_map.get((h, p))
        code = _general_codes.get((h, p))
        if flag and code:
            return {
                "host": h,
                "port": p,
                "match_host": "",
                "country_code": code,
                "flag": flag,
                # Exact only when this row itself is the general (empty match) rule
                "exact": not bool(m),
            }
    return None


def resolve_location_for_link(link: str) -> dict[str, str] | None:
    ep = extract_link_endpoint(link)
    if not ep:
        return None
    server_host, port = ep
    link_match = extract_link_match_host(link) or ""
    return resolve_location_for_endpoint(server_host, port, link_match)


def apply_cache(rows: list[ConfigLocation]) -> None:
    specific: dict[tuple[str, int, str], str] = {}
    specific_codes: dict[tuple[str, int, str], str] = {}
    general: dict[tuple[str, int], str] = {}
    general_codes: dict[tuple[str, int], str] = {}
    for row in rows:
        try:
            host = normalize_host(row.host)
            port = int(row.port)
            match = normalize_match_host(getattr(row, "match_host", None) or "")
            code = normalize_country_code(row.country_code)
            flag = flag_from_country_code(code)
        except (ValueError, TypeError):
            continue
        if match:
            specific[(host, port, match)] = flag
            specific_codes[(host, port, match)] = code
        else:
            general[(host, port)] = flag
            general_codes[(host, port)] = code
    with _lock:
        global _specific_map, _specific_codes, _general_map, _general_codes, _loaded
        _specific_map = specific
        _specific_codes = specific_codes
        _general_map = general
        _general_codes = general_codes
        _loaded = True


async def refresh_cache(db: AsyncSession) -> int:
    result = await db.execute(select(ConfigLocation))
    rows = list(result.scalars().all())
    apply_cache(rows)
    return len(rows)


async def upsert_endpoint_location(
    db: AsyncSession,
    *,
    host: str,
    port: int,
    match_host: str = "",
    country_code: str | None,
) -> dict | None:
    """Set or clear the location rule for an exact (host, port, match_host) fingerprint.

    ``country_code=None`` deletes that exact rule only (does not touch other SNI variants).
    Returns the saved row as ``to_out`` dict, or ``None`` when cleared.
    """
    h = normalize_host(host)
    p = int(port)
    if p < 1 or p > 65535:
        raise ValueError("پورت نامعتبر است")
    m = normalize_match_host(match_host or "")

    result = await db.execute(
        select(ConfigLocation).where(
            ConfigLocation.host == h,
            ConfigLocation.port == p,
            ConfigLocation.match_host == m,
        )
    )
    row = result.scalar_one_or_none()

    if country_code is None or str(country_code).strip() == "":
        if row is not None:
            await db.delete(row)
            await db.commit()
            await refresh_cache(db)
        else:
            await db.commit()
        return None

    code = normalize_country_code(country_code)
    flag_from_country_code(code)  # validate
    if row is None:
        row = ConfigLocation(host=h, port=p, match_host=m, country_code=code, label=None)
        db.add(row)
    else:
        row.country_code = code
    await db.commit()
    await db.refresh(row)
    await refresh_cache(db)
    return to_out(row)


def attach_location_to_rows(rows: list[dict]) -> None:
    """Mutate visibility rows with country_code + flag (same resolve rules as sub)."""
    for r in rows:
        info = resolve_location_for_endpoint(
            str(r.get("host") or ""),
            int(r.get("port") or 0),
            str(r.get("match_host") or ""),
        )
        if info:
            r["country_code"] = info["country_code"]
            r["flag"] = info["flag"]
            r["flag_exact"] = bool(info.get("exact"))
        else:
            r["country_code"] = None
            r["flag"] = None
            r["flag_exact"] = False


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
