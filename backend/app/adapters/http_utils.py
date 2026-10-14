"""Shared HTTP helpers for panel adapters."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.config import get_settings

settings = get_settings()

# UI path suffixes users often paste from the browser address bar
_UI_SUFFIXES = (
    "/dashboard",
    "/admin",
    "/panel",
    "/ui",
    "/app",
    "/#",
)


def normalize_panel_base_url(url: str) -> str:
    """
    Normalize panel base URL to the API root.
    Example: https://host:8443/dashboard  →  https://host:8443
    """
    raw = (url or "").strip()
    if not raw:
        return raw
    parts = urlsplit(raw)
    path = parts.path or ""
    # drop trailing slash / hash fragments in path
    while path.endswith("/"):
        path = path[:-1]
    lower = path.lower()
    for suffix in _UI_SUFFIXES:
        if lower.endswith(suffix):
            path = path[: -len(suffix)]
            lower = path.lower()
            break
    # if someone pasted .../api or .../api/admin keep host only for adapters that append /api/...
    for api_tail in ("/api/admin", "/api"):
        if lower.endswith(api_tail):
            path = path[: -len(api_tail)]
            break
    while path.endswith("/"):
        path = path[:-1]
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")).rstrip("/")


def make_client(base_url: str, verify_ssl: bool = False) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=normalize_panel_base_url(base_url),
        timeout=settings.ADAPTER_TIMEOUT,
        verify=verify_ssl,
        follow_redirects=True,
    )


def ok_json(resp: httpx.Response) -> tuple[bool, Any]:
    try:
        data = resp.json()
    except Exception:
        return False, {"text": resp.text[:300]}
    if resp.status_code >= 400:
        return False, data
    if isinstance(data, dict) and data.get("success") is False:
        return False, data
    return True, data


def err_msg(data: Any, fallback: str = "Request failed") -> str:
    if isinstance(data, dict):
        detail = data.get("msg") or data.get("detail") or data.get("error")
        if isinstance(detail, list):
            parts = []
            for item in detail:
                if isinstance(item, dict):
                    parts.append(str(item.get("msg") or item.get("detail") or item))
                else:
                    parts.append(str(item))
            detail = "; ".join(parts) if parts else None
        return str(detail or fallback)[:400]
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


async def expand_subscription_links(
    client: httpx.AsyncClient,
    links: list[str],
    *,
    headers: dict[str, str] | None = None,
) -> list[str]:
    """If only http(s) subscription URLs are present, fetch and split into share links."""
    import base64

    links = [str(x).strip() for x in (links or []) if x and str(x).strip()]
    if not links:
        return []
    existing_share = [l for l in links if l.lower().startswith(SHARE_LINK_PREFIXES)]
    if existing_share:
        return existing_share

    subs = [l for l in links if l.lower().startswith(("http://", "https://"))]
    if not subs:
        return links

    out: list[str] = []
    req_headers = {"User-Agent": "AstraPanel/1.0", **(headers or {})}
    for url in subs:
        try:
            resp = await client.get(url, headers=req_headers)
            if resp.status_code >= 400:
                continue
            text = (resp.text or "").strip()
            if not text:
                continue
            decoded = text
            try:
                pad = "=" * (-len(text) % 4)
                decoded = base64.urlsafe_b64decode(text + pad).decode("utf-8", errors="ignore")
            except Exception:
                decoded = text
            for line in decoded.replace("\r", "\n").split("\n"):
                line = line.strip()
                if line and line.lower().startswith(SHARE_LINK_PREFIXES):
                    out.append(line)
        except Exception:
            continue
    return out or links
