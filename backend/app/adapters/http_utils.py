"""Shared HTTP helpers for panel adapters."""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.config import get_settings

settings = get_settings()

# UI tails users paste from the address bar (after webBasePath). Keep /moj etc.
_UI_TAILS = (
    "/panel/inbounds",
    "/panel/api-docs",
    "/panel/settings",
    "/dashboard",
    "/admin",
    "/panel",
    "/ui",
    "/app",
)


def normalize_panel_base_url(url: str) -> str:
    """
    Normalize panel base URL to the API root (keep webBasePath).
    Example: https://host:2053/moj/panel  →  https://host:2053/moj
    Example: https://host:2053/moj/       →  https://host:2053/moj
    """
    raw = (url or "").strip()
    if not raw:
        return raw
    parts = urlsplit(raw)
    path = (parts.path or "").rstrip("/")
    lower = path.lower()
    for tail in _UI_TAILS:
        if lower.endswith(tail):
            path = path[: -len(tail)].rstrip("/")
            lower = path.lower()
            break
    for api_tail in ("/api/admin", "/api"):
        if lower.endswith(api_tail):
            path = path[: -len(api_tail)].rstrip("/")
            break
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")).rstrip("/")

# One shared AsyncClient per panel base URL — stops Errno 24 (Too many open files)
# when usage refresh / freeze / reset stampede get_adapter() × unbounded clients.
_SHARED_CLIENTS: dict[str, httpx.AsyncClient] = {}
_SHARED_LOCK = asyncio.Lock()
# Cap sockets per panel; default httpx limits are huge and exhaust process FDs.
# Worker hosts many panels on the SAME base_url (Direct+Tunnel twins) — 32 was
# too low under live get_usage stampede and caused httpx.PoolTimeout, which then
# starved cold /api/users sweeps so meter_cache went hours stale.
_HTTP_LIMITS = httpx.Limits(max_connections=96, max_keepalive_connections=48)


def _shared_client_key(base_url: str, verify_ssl: bool, xhr: bool) -> str:
    root = normalize_panel_base_url(base_url).rstrip("/")
    return f"{root}|v{int(bool(verify_ssl))}|x{int(bool(xhr))}"


def make_client(
    base_url: str,
    verify_ssl: bool = False,
    *,
    timeout: float | None = None,
    xhr: bool = True,
) -> httpx.AsyncClient:
    """
    httpx client rooted at panel base (including webBasePath).

    X-Requested-With helps 3x-ui return real 401 for bad tokens (otherwise often 404).
    Marzban/PasarGuard/Rebecca should pass xhr=False — some builds are picky about extra headers on /api/admin/token.

    Prefer ``acquire_shared_client`` for adapters so concurrent ops reuse one pool.
    """
    root = normalize_panel_base_url(base_url).rstrip("/") + "/"
    t = float(timeout if timeout is not None else settings.ADAPTER_TIMEOUT)
    # Fail-fast connect — slow handshakes must not block test-all / usage waves.
    connect_t = min(2.5, max(1.5, t * 0.08))
    timeout_cfg = httpx.Timeout(t, connect=connect_t)
    headers = {
        "User-Agent": "AstraPanel/1.1 (httpx)",
        "Accept": "application/json, text/plain, */*",
    }
    if xhr:
        headers["X-Requested-With"] = "XMLHttpRequest"
    return httpx.AsyncClient(
        base_url=root,
        timeout=timeout_cfg,
        verify=verify_ssl,
        follow_redirects=True,
        headers=headers,
        limits=_HTTP_LIMITS,
    )


async def acquire_shared_client(
    base_url: str,
    verify_ssl: bool = False,
    *,
    timeout: float | None = None,
    xhr: bool = True,
) -> httpx.AsyncClient:
    """Process-wide client for a panel URL (safe across adapter instances)."""
    key = _shared_client_key(base_url, verify_ssl, xhr)
    async with _SHARED_LOCK:
        client = _SHARED_CLIENTS.get(key)
        if client is None or client.is_closed:
            client = make_client(
                base_url, verify_ssl, timeout=timeout, xhr=xhr
            )
            _SHARED_CLIENTS[key] = client
        return client


async def invalidate_shared_client(
    base_url: str,
    verify_ssl: bool = False,
    *,
    xhr: bool = True,
) -> None:
    """Drop pooled client (e.g. after EMFILE / broken TLS) so next acquire is fresh."""
    key = _shared_client_key(base_url, verify_ssl, xhr)
    async with _SHARED_LOCK:
        client = _SHARED_CLIENTS.pop(key, None)
    if client is None:
        return
    try:
        if not client.is_closed:
            await client.aclose()
    except Exception:  # noqa: BLE001
        pass


def is_emfile_err(exc: BaseException | str | None) -> bool:
    text = str(exc or "")
    return "Too many open files" in text or "Errno 24" in text


def ok_json(resp: httpx.Response) -> tuple[bool, Any]:
    try:
        data = resp.json()
    except Exception:
        text = (resp.text or "")[:300]
        return False, {"text": text, "status_code": resp.status_code}
    if resp.status_code >= 400:
        return False, data if isinstance(data, dict) else {"data": data, "status_code": resp.status_code}
    if isinstance(data, dict) and data.get("success") is False:
        return False, data
    return True, data


def err_msg(data: Any, fallback: str = "Request failed") -> str:
    if isinstance(data, dict):
        detail = data.get("msg") or data.get("detail") or data.get("error") or data.get("text")
        if isinstance(detail, list):
            parts = []
            for item in detail:
                if isinstance(item, dict):
                    parts.append(str(item.get("msg") or item.get("detail") or item))
                else:
                    parts.append(str(item))
            detail = "; ".join(parts) if parts else None
        code = data.get("status_code")
        msg = str(detail or fallback)[:400]
        if code and str(code) not in msg:
            return f"{msg} (HTTP {code})"
        return msg
    return str(data)[:400]


SHARE_LINK_PREFIXES = (
    "vless://",
    "vmess://",
    "trojan://",
    "ss://",
    "shadowsocks://",
    "tuic://",
    "hysteria://",
    "hy2://",
)


def is_share_link(link: str) -> bool:
    return bool(link) and str(link).strip().lower().startswith(SHARE_LINK_PREFIXES)


def is_subscription_ref(link: str) -> bool:
    """http(s) sub URL or relative panel path like /sub/TOKEN (PasarGuard/Marzban)."""
    text = str(link or "").strip()
    if not text:
        return False
    low = text.lower()
    if low.startswith(SHARE_LINK_PREFIXES):
        return False
    if low.startswith(("http://", "https://")):
        return True
    if low.startswith("/sub/") or low.startswith("sub/"):
        return True
    return False


def _decode_sub_body(text: str) -> str:
    import base64

    raw = (text or "").strip()
    if not raw:
        return ""
    # Already plain share links / multi-line
    if any(
        line.strip().lower().startswith(SHARE_LINK_PREFIXES)
        for line in raw.replace("\r", "\n").split("\n")
    ):
        return raw
    # Some panels wrap base64 with whitespace / newlines
    candidates = [raw, re.sub(r"\s+", "", raw)]
    for blob in candidates:
        pad = "=" * (-len(blob) % 4)
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                dec = decoder(blob + pad).decode("utf-8", errors="ignore")
            except Exception:
                continue
            if not dec:
                continue
            # Prefer decoded text that actually looks like share links
            if any(
                line.strip().lower().startswith(SHARE_LINK_PREFIXES)
                for line in dec.replace("\r", "\n").split("\n")
            ) or "://" in dec:
                return dec
    return raw


_SHARE_LINK_RE = re.compile(
    r"(?:vless|vmess|trojan|ss|shadowsocks|tuic|hysteria2?|hy2)://[^\s<>\"']+",
    re.IGNORECASE,
)


def extract_share_links(text: str) -> list[str]:
    """Unwrap plain / base64 / embedded share-links from a subscription body."""
    decoded = _decode_sub_body(text)
    out: list[str] = []
    seen: set[str] = set()

    def _add(link: str) -> None:
        line = (link or "").strip().rstrip("\",;")
        if not line or not is_share_link(line) or line in seen:
            return
        seen.add(line)
        out.append(line)

    for line in decoded.replace("\r", "\n").split("\n"):
        _add(line.strip())
    # Fallback: links embedded in HTML / JSON strings / one long line
    if not out:
        for m in _SHARE_LINK_RE.finditer(decoded):
            _add(m.group(0))
    if not out and text and text is not decoded:
        for m in _SHARE_LINK_RE.finditer(text):
            _add(m.group(0))
    return out


async def expand_subscription_links(
    client: httpx.AsyncClient | None,
    links: list[str],
    *,
    headers: dict[str, str] | None = None,
) -> list[str]:
    """
    Turn panel subscription refs into real share links for V2Ray clients.

    PasarGuard/Marzban often return only `/sub/<token>` or an https sub URL —
    clients cannot import those; we must fetch and unwrap to vless:// etc.

    Absolute http(s) URLs are fetched with a dedicated client (no panel base),
    so CDN/sub-domain hosts work even when panel base_url differs.
    """
    links = [str(x).strip() for x in (links or []) if x and str(x).strip()]
    if not links:
        return []
    existing_share = [l for l in links if is_share_link(l)]
    subs = [l for l in links if is_subscription_ref(l)]
    # PasarGuard/Marzban often return a few share links (or notice vless) PLUS
    # subscription_url with the full set. Always expand /sub when present.
    if not subs:
        return existing_share

    out: list[str] = []
    seen: set[str] = set()
    for line in existing_share:
        if line not in seen:
            seen.add(line)
            out.append(line)
    req_headers = {
        # Prefer share-link body (base64); some panels give JSON for v2rayNG UA
        "User-Agent": "v2rayN/6.23",
        "Accept": "text/plain, */*",
        **(headers or {}),
    }
    # Large Marzban/PasarGuard /sub bodies (100–300 configs) need a real read
    # window. Too-short reads abort mid-body → clients only saw ~30 configs.
    sub_timeout = httpx.Timeout(18.0, connect=3.0)

    async def _fetch_absolute(url: str) -> list[str]:
        abs_timeout = httpx.Timeout(18.0, connect=3.0)
        # PasarGuard sometimes stores https://host:port//sub/TOKEN (404).
        try:
            parts = urlsplit(url)
            path = parts.path or ""
            while "//" in path:
                path = path.replace("//", "/")
            url = urlunsplit(
                (parts.scheme, parts.netloc, path, parts.query, parts.fragment)
            )
        except Exception:
            pass
        try:
            async with httpx.AsyncClient(
                timeout=abs_timeout,
                verify=False,
                follow_redirects=True,
                headers=req_headers,
            ) as bare:
                resp = await bare.get(url)
                if resp.status_code >= 400:
                    return []
                text = (resp.text or "").strip()
                if not text:
                    return []
                found = extract_share_links(text)
                if found:
                    return found
                try:
                    from app.services.outbound_uri import share_links_from_xray_json

                    return share_links_from_xray_json(text)
                except Exception:
                    return []
        except Exception:
            return []

    async def _fetch_relative(url: str) -> list[str]:
        if client is None:
            return []
        try:
            path = url
            if path.lower().startswith("sub/") and not path.lower().startswith(
                ("http://", "https://")
            ):
                path = "/" + path
            resp = await client.get(path, headers=req_headers, timeout=sub_timeout)
            if resp.status_code >= 400:
                return []
            text = (resp.text or "").strip()
            if not text:
                return []
            found = extract_share_links(text)
            if found:
                return found
            try:
                from app.services.outbound_uri import share_links_from_xray_json

                return share_links_from_xray_json(text)
            except Exception:
                return []
        except Exception:
            return []

    def _relative_sub_path(url: str) -> str | None:
        """https://cdn/sub/TOKEN → /sub/TOKEN (fallback via panel base)."""
        try:
            parts = urlsplit(url)
        except Exception:
            return None
        path = parts.path or ""
        low = path.lower()
        idx = low.find("/sub/")
        if idx < 0:
            if low.startswith("sub/"):
                path = "/" + path
                idx = 0
            else:
                return None
            idx = path.lower().find("/sub/")
        if idx < 0:
            return None
        rel = path[idx:]
        if parts.query:
            rel = f"{rel}?{parts.query}"
        return rel

    async def _fetch_one(url: str) -> list[str]:
        low = url.lower()
        if low.startswith(("http://", "https://")):
            got = await _fetch_absolute(url)
            if got:
                return got
            # CDN/sub-domain unreachable from server → try same /sub path on panel
            rel = _relative_sub_path(url)
            if rel:
                return await _fetch_relative(rel)
            return []
        return await _fetch_relative(url)

    batches = await asyncio.gather(*[_fetch_one(u) for u in subs], return_exceptions=True)
    for lines in batches:
        if isinstance(lines, BaseException):
            continue
        for line in lines:
            if line not in seen:
                seen.add(line)
                out.append(line)
    return out
