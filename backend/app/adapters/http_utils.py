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
) -> httpx.AsyncClient:
    """
    httpx client rooted at panel base (including webBasePath).

    Always send browser-like headers so 3x-ui returns real 401 for bad tokens
    (without X-Requested-With it often masks auth failure as 404).
    """
    root = normalize_panel_base_url(base_url).rstrip("/") + "/"
    t = float(timeout if timeout is not None else settings.ADAPTER_TIMEOUT)
    # Connect fast; allow longer read for heavy panels (3x-ui full inbound list)
    timeout_cfg = httpx.Timeout(t, connect=min(15.0, t))
    return httpx.AsyncClient(
        base_url=root,
        timeout=timeout_cfg,
        verify=verify_ssl,
        follow_redirects=True,
        headers={
            "User-Agent": "AstraPanel/1.1 (httpx)",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        },
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
    req_headers = {"User-Agent": "AstraPanel/1.1", **(headers or {})}
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
