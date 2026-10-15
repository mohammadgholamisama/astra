"""Shared HTTP helpers for panel adapters."""

from __future__ import annotations

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
    """
    root = normalize_panel_base_url(base_url).rstrip("/") + "/"
    t = float(timeout if timeout is not None else settings.ADAPTER_TIMEOUT)
    # Keep connect short so flaky hosts (Marzban Cloud) fail fast and retry,
    # instead of burning 30s×N inside a single attempt.
    connect_t = min(8.0, max(4.0, t * 0.25))
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
    )


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
    if any(line.strip().lower().startswith(SHARE_LINK_PREFIXES) for line in raw.replace("\r", "\n").split("\n")):
        return raw
    pad = "=" * (-len(raw) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return decoder(raw + pad).decode("utf-8", errors="ignore")
        except Exception:
            continue
    return raw


def extract_share_links(text: str) -> list[str]:
    decoded = _decode_sub_body(text)
    out: list[str] = []
    seen: set[str] = set()
    for line in decoded.replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line or not is_share_link(line) or line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


async def expand_subscription_links(
    client: httpx.AsyncClient,
    links: list[str],
    *,
    headers: dict[str, str] | None = None,
) -> list[str]:
    """
    Turn panel subscription refs into real share links for V2Ray clients.

    PasarGuard/Marzban often return only `/sub/<token>` or an https sub URL —
    clients cannot import those; we must fetch and unwrap to vless:// etc.
    """
    links = [str(x).strip() for x in (links or []) if x and str(x).strip()]
    if not links:
        return []
    existing_share = [l for l in links if is_share_link(l)]
    if existing_share:
        return existing_share

    subs = [l for l in links if is_subscription_ref(l)]
    if not subs:
        # Never return bare /sub/... junk to clients
        return []

    out: list[str] = []
    seen: set[str] = set()
    req_headers = {
        "User-Agent": "v2rayNG/1.8 (AstraPanel)",
        "Accept": "text/plain, */*",
        **(headers or {}),
    }
    # Short per-sub timeout — never burn the whole ADAPTER_TIMEOUT on one /sub fetch
    sub_timeout = httpx.Timeout(12.0, connect=10.0)
    for url in subs:
        try:
            path = url
            if path.lower().startswith("sub/") and not path.lower().startswith(("http://", "https://")):
                path = "/" + path
            resp = await client.get(path, headers=req_headers, timeout=sub_timeout)
            if resp.status_code >= 400:
                continue
            text = (resp.text or "").strip()
            if not text:
                continue
            for line in extract_share_links(text):
                if line not in seen:
                    seen.add(line)
                    out.append(line)
        except Exception:
            continue
    return out
