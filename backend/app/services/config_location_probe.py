"""Fast country/flag probe for config-order rows (remark emoji + GeoIP batch)."""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from typing import Any

import httpx

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.config_locations import (
    flag_from_country_code,
    normalize_country_code,
    normalize_host,
    normalize_match_host,
    upsert_endpoint_locations_many,
)

logger = logging.getLogger("astra.config_location_probe")

_IPV4 = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
# Regional-indicator pairs → ISO country (🇩🇪 → DE)
_FLAG_PAIR = re.compile(
    "([\U0001F1E6-\U0001F1FF]{2})"
)

# Process-local cache: host → country code (short session)
_host_country_cache: dict[str, str] = {}
_cache_lock = asyncio.Lock()


def country_code_from_flag_emoji(text: str | None) -> str | None:
    """First flag emoji in remark/label → ISO alpha-2."""
    raw = text or ""
    m = _FLAG_PAIR.search(raw)
    if not m:
        return None
    pair = m.group(1)
    try:
        c0 = ord(pair[0]) - 0x1F1E6
        c1 = ord(pair[1]) - 0x1F1E6
        if not (0 <= c0 <= 25 and 0 <= c1 <= 25):
            return None
        code = chr(65 + c0) + chr(65 + c1)
        return normalize_country_code(code)
    except (ValueError, TypeError, IndexError):
        return None


def _is_ip(host: str) -> bool:
    h = (host or "").strip().lower().strip("[]")
    if _IPV4.match(h):
        return True
    if ":" in h and h.count(":") >= 2:
        return True
    return False


async def _resolve_one(host: str) -> str | None:
    h = (host or "").strip().lower().strip("[]")
    if not h:
        return None
    if _is_ip(h):
        return h.split("%", 1)[0]
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            h,
            None,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        return None
    # Prefer IPv4 for broader GeoIP coverage
    v4: str | None = None
    v6: str | None = None
    for info in infos:
        addr = info[4][0]
        if ":" in addr:
            if v6 is None:
                v6 = addr
        elif v4 is None:
            v4 = addr
    return v4 or v6


async def resolve_hosts_to_ips(hosts: list[str]) -> dict[str, str]:
    """Parallel DNS — unique hosts only."""
    uniq = []
    seen: set[str] = set()
    for h in hosts:
        key = (h or "").strip().lower().strip("[]")
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(key)
    if not uniq:
        return {}
    sem = asyncio.Semaphore(40)

    async def _one(host: str) -> tuple[str, str | None]:
        async with sem:
            return host, await _resolve_one(host)

    pairs = await asyncio.gather(*[_one(h) for h in uniq])
    return {h: ip for h, ip in pairs if ip}


async def _geo_batch_ip_api(ips: list[str]) -> dict[str, str]:
    """ip-api.com batch (HTTP, up to 100 IPs / request)."""
    out: dict[str, str] = {}
    if not ips:
        return out
    timeout = httpx.Timeout(12.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for i in range(0, len(ips), 100):
            chunk = ips[i : i + 100]
            try:
                resp = await client.post(
                    "http://ip-api.com/batch",
                    params={"fields": "status,countryCode,query"},
                    json=chunk,
                )
                if resp.status_code == 429:
                    # brief backoff once
                    await asyncio.sleep(1.2)
                    resp = await client.post(
                        "http://ip-api.com/batch",
                        params={"fields": "status,countryCode,query"},
                        json=chunk,
                    )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                logger.warning("ip-api batch failed: %s", exc)
                continue
            if not isinstance(data, list):
                continue
            for row, ip in zip(data, chunk):
                if not isinstance(row, dict):
                    continue
                if str(row.get("status") or "") != "success":
                    continue
                code = str(row.get("countryCode") or "").strip().upper()
                if len(code) == 2 and code.isalpha():
                    out[ip] = code
                    q = str(row.get("query") or "").strip()
                    if q:
                        out[q] = code
    return out


async def _geo_fallback_ipwho(ips: list[str]) -> dict[str, str]:
    """HTTPS fallback for IPs still missing after ip-api."""
    out: dict[str, str] = {}
    if not ips:
        return out
    sem = asyncio.Semaphore(12)
    timeout = httpx.Timeout(8.0, connect=4.0)

    async def _one(client: httpx.AsyncClient, ip: str) -> tuple[str, str | None]:
        async with sem:
            try:
                r = await client.get(f"https://ipwho.is/{ip}")
                if r.status_code != 200:
                    return ip, None
                data = r.json()
                if not isinstance(data, dict) or not data.get("success", True):
                    return ip, None
                code = str(data.get("country_code") or "").strip().upper()
                if len(code) == 2 and code.isalpha():
                    return ip, code
            except Exception:  # noqa: BLE001
                return ip, None
        return ip, None

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        pairs = await asyncio.gather(*[_one(client, ip) for ip in ips])
    for ip, code in pairs:
        if code:
            out[ip] = code
    return out


async def countries_for_hosts(hosts: list[str]) -> dict[str, str]:
    """host → ISO country. Uses cache, DNS, then GeoIP batch."""
    result: dict[str, str] = {}
    need_resolve: list[str] = []
    async with _cache_lock:
        for h in hosts:
            key = (h or "").strip().lower().strip("[]")
            if not key:
                continue
            cached = _host_country_cache.get(key)
            if cached:
                result[key] = cached
            else:
                need_resolve.append(key)
    if not need_resolve:
        return result

    host_ip = await resolve_hosts_to_ips(need_resolve)
    ips = sorted({ip for ip in host_ip.values() if ip})
    ip_country = await _geo_batch_ip_api(ips)
    missing = [ip for ip in ips if ip not in ip_country]
    if missing:
        ip_country.update(await _geo_fallback_ipwho(missing[:80]))

    async with _cache_lock:
        for host, ip in host_ip.items():
            code = ip_country.get(ip)
            if not code:
                continue
            try:
                code = normalize_country_code(code)
            except ValueError:
                continue
            _host_country_cache[host] = code
            result[host] = code
    return result


async def probe_config_flags(
    db: AsyncSession,
    items: list[dict[str, Any]],
    *,
    overwrite: bool = True,
) -> dict[str, Any]:
    """Resolve country for each fingerprint and upsert location rules.

    Priority per row: flag emoji in remark/label → GeoIP of server host.
    ``overwrite=True`` replaces existing flags (re-probe).
    """
    normalized: list[dict[str, Any]] = []
    for raw in items:
        try:
            host = normalize_host(str(raw.get("host") or ""))
            port = int(raw.get("port") or 0)
            match = normalize_match_host(str(raw.get("match_host") or ""))
        except (ValueError, TypeError):
            continue
        if port < 1 or port > 65535:
            continue
        normalized.append(
            {
                "host": host,
                "port": port,
                "match_host": match,
                "source_remark": (raw.get("source_remark") or raw.get("label") or "")
                or "",
                "label": (raw.get("label") or "") or "",
                "had_flag": bool(raw.get("country_code")),
            }
        )

    # Decide country per row
    pending_geo_hosts: list[str] = []
    planned: list[dict[str, Any]] = []
    for it in normalized:
        code = country_code_from_flag_emoji(str(it["source_remark"]))
        source = "remark"
        if not code:
            code = country_code_from_flag_emoji(str(it["label"]))
        if not code:
            pending_geo_hosts.append(it["host"])
            source = "geo"
        planned.append({**it, "country_code": code, "source": source})

    host_codes = await countries_for_hosts(pending_geo_hosts) if pending_geo_hosts else {}

    upserts: list[tuple[str, int, str, str]] = []
    results: list[dict[str, Any]] = []
    for it in planned:
        code = it.get("country_code")
        source = str(it.get("source") or "geo")
        if not code:
            code = host_codes.get(it["host"])
            source = "geo" if code else "none"
        if not code:
            results.append(
                {
                    "host": it["host"],
                    "port": it["port"],
                    "match_host": it["match_host"],
                    "country_code": None,
                    "flag": None,
                    "source": "failed",
                    "ok": False,
                    "detail": "کشور پیدا نشد",
                }
            )
            continue
        if not overwrite and it.get("had_flag"):
            # Should not happen for our UI (always overwrite on probe)
            results.append(
                {
                    "host": it["host"],
                    "port": it["port"],
                    "match_host": it["match_host"],
                    "country_code": None,
                    "flag": None,
                    "source": "skipped",
                    "ok": True,
                    "detail": "از قبل پرچم داشت",
                }
            )
            continue
        try:
            code = normalize_country_code(str(code))
            flag = flag_from_country_code(code)
        except ValueError:
            results.append(
                {
                    "host": it["host"],
                    "port": it["port"],
                    "match_host": it["match_host"],
                    "country_code": None,
                    "flag": None,
                    "source": "failed",
                    "ok": False,
                    "detail": "کد کشور نامعتبر",
                }
            )
            continue
        upserts.append((it["host"], it["port"], it["match_host"], code))
        results.append(
            {
                "host": it["host"],
                "port": it["port"],
                "match_host": it["match_host"],
                "country_code": code,
                "flag": flag,
                "source": source,
                "ok": True,
                "detail": None,
            }
        )

    saved = 0
    if upserts:
        outs = await upsert_endpoint_locations_many(db, upserts)
        saved = len(outs)

    ok_n = sum(1 for r in results if r.get("ok") and r.get("country_code"))
    fail_n = sum(1 for r in results if not r.get("ok"))
    return {
        "ok": True,
        "updated": saved,
        "resolved": ok_n,
        "failed": fail_n,
        "total": len(normalized),
        "results": results,
    }
