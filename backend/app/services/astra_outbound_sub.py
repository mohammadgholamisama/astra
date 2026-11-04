"""Import / sync Astra outbounds from an upstream subscription URL."""

from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.http_utils import extract_share_links, is_share_link
from app.models.astra_outbound import AstraOutbound
from app.models.panel import Panel
from app.services.outbound_uri import parse_outbound_lines

logger = logging.getLogger("astra.outbound_sub")

_META_URL = "outbound_sub_url"
_META_TITLE = "outbound_sub_title"
_META_UPLOAD = "outbound_sub_upload"
_META_DOWNLOAD = "outbound_sub_download"
_META_TOTAL = "outbound_sub_total"
_META_EXPIRE = "outbound_sub_expire"
_META_SYNCED = "outbound_sub_synced_at"
_META_ERROR = "outbound_sub_error"
_META_COUNT = "outbound_sub_count"

_MAX_LINKS = 200


def _panel_meta(panel: Panel) -> dict[str, Any]:
    raw = getattr(panel, "meta", None)
    return dict(raw) if isinstance(raw, dict) else {}


def _set_meta(panel: Panel, **kwargs: Any) -> None:
    meta = _panel_meta(panel)
    for k, v in kwargs.items():
        if v is None:
            meta.pop(k, None)
        else:
            meta[k] = v
    panel.meta = meta


def subscription_info(panel: Panel) -> dict[str, Any] | None:
    meta = _panel_meta(panel)
    url = str(meta.get(_META_URL) or "").strip()
    if not url:
        return None
    return {
        "url": url,
        "title": str(meta.get(_META_TITLE) or "").strip() or None,
        "upload": int(meta.get(_META_UPLOAD) or 0),
        "download": int(meta.get(_META_DOWNLOAD) or 0),
        "total": int(meta.get(_META_TOTAL) or 0),
        "expire": int(meta.get(_META_EXPIRE) or 0),
        "synced_at": str(meta.get(_META_SYNCED) or "") or None,
        "error": str(meta.get(_META_ERROR) or "") or None,
        "count": int(meta.get(_META_COUNT) or 0),
    }


def clear_subscription_meta(panel: Panel) -> None:
    _set_meta(
        panel,
        **{
            _META_URL: None,
            _META_TITLE: None,
            _META_UPLOAD: None,
            _META_DOWNLOAD: None,
            _META_TOTAL: None,
            _META_EXPIRE: None,
            _META_SYNCED: None,
            _META_ERROR: None,
            _META_COUNT: None,
        },
    )


def _decode_header_title(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    # profile-title: base64:...
    low = text.lower()
    if low.startswith("base64:"):
        b64 = text.split(":", 1)[1].strip()
        try:
            pad = "=" * (-len(b64) % 4)
            return base64.b64decode(b64 + pad).decode("utf-8", "ignore").strip()
        except Exception:  # noqa: BLE001
            return b64
    # Sometimes double-encoded or plain
    try:
        if re.fullmatch(r"[A-Za-z0-9+/=\s]+", text) and len(text) >= 8:
            pad = "=" * (-len(text) % 4)
            dec = base64.b64decode(text + pad).decode("utf-8", "ignore").strip()
            if dec and not dec.isascii() or " " in dec or len(dec) > 2:
                # Prefer decoded when it looks like a title
                if any(ord(c) > 127 for c in dec) or " " in dec:
                    return dec
    except Exception:  # noqa: BLE001
        pass
    return unquote(text).strip()


def _parse_userinfo(header: str) -> dict[str, int]:
    out = {"upload": 0, "download": 0, "total": 0, "expire": 0}
    raw = (header or "").strip()
    if not raw:
        return out
    for part in raw.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        key = k.strip().lower()
        try:
            num = int(float(str(v).strip() or 0))
        except (TypeError, ValueError):
            continue
        if key in out:
            out[key] = max(0, num)
    return out


async def fetch_subscription(url: str) -> tuple[list[str], dict[str, Any]]:
    """Return (share_links, meta) from a subscription URL."""
    u = (url or "").strip()
    if not u:
        raise ValueError("آدرس ساب خالی است")
    if not (u.startswith("http://") or u.startswith("https://")):
        raise ValueError("آدرس ساب باید با http یا https شروع شود")

    headers = {
        "User-Agent": "v2rayNG/1.8.5 (AstraOutboundSync)",
        "Accept": "text/plain,*/*",
    }
    timeout = httpx.Timeout(25.0, connect=8.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        verify=False,
        follow_redirects=True,
        headers=headers,
    ) as client:
        resp = await client.get(u)
        if resp.status_code >= 400:
            raise ValueError(f"دریافت ساب ناموفق بود ({resp.status_code})")
        body = (resp.text or "").strip()
        rh = {k.lower(): v for k, v in resp.headers.items()}

    title = _decode_header_title(
        rh.get("profile-title") or rh.get("content-disposition") or ""
    )
    # content-disposition filename fallback
    if not title and "filename=" in (rh.get("content-disposition") or "").lower():
        m = re.search(
            r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?',
            rh.get("content-disposition") or "",
            re.I,
        )
        if m:
            title = unquote(m.group(1)).strip()
            if title.lower().endswith(".txt"):
                title = title[:-4]

    info = _parse_userinfo(rh.get("subscription-userinfo") or "")
    links = extract_share_links(body)
    if not links and is_share_link(body.splitlines()[0] if body else ""):
        links = [body.splitlines()[0].strip()]
    if not links:
        raise ValueError("داخل این ساب لینک کانفیگ معتبری پیدا نشد")

    return links[:_MAX_LINKS], {
        "title": title or None,
        "upload": info["upload"],
        "download": info["download"],
        "total": info["total"],
        "expire": info["expire"],
    }


async def sync_outbounds_from_subscription(
    db: AsyncSession,
    panel: Panel,
    url: str,
    *,
    ping: bool = False,
) -> dict[str, Any]:
    """Fetch sub, upsert outbounds by uri_hash, drop missing, store meta on panel."""
    from app.services import astra_node as node

    links, meta = await fetch_subscription(url)
    text = "\n".join(links)
    try:
        parsed_list = parse_outbound_lines(text)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    if not parsed_list:
        raise ValueError("لینک معتبری از ساب استخراج نشد")

    existing = await node.list_outbounds(db, panel.id)
    by_hash = {str(r.uri_hash): r for r in existing}
    keep_hashes: set[str] = set()
    created = 0
    updated = 0
    title = (meta.get("title") or "").strip()

    for i, p in enumerate(parsed_list):
        keep_hashes.add(p.uri_hash)
        remark = (p.remark or "").strip()
        if title and remark and not remark.startswith(title):
            # Keep original remark; title shown on subscription card
            pass
        row = by_hash.get(p.uri_hash)
        if row is None:
            row = AstraOutbound(
                panel_id=panel.id,
                uri=p.uri,
                uri_hash=p.uri_hash,
                remark=remark[:255] or f"{p.protocol}-{p.host}",
                protocol=p.protocol,
                host=p.host,
                port=p.port,
                parsed=p.raw,
                is_enabled=True,
                sort_order=i + 1,
            )
            db.add(row)
            created += 1
        else:
            row.uri = p.uri
            row.remark = (remark[:255] or row.remark or "")[:255]
            row.protocol = p.protocol
            row.host = p.host
            row.port = p.port
            row.parsed = p.raw
            row.sort_order = i + 1
            updated += 1

    removed = 0
    for h, row in by_hash.items():
        if h not in keep_hashes:
            await db.delete(row)
            removed += 1

    now = datetime.now(timezone.utc).isoformat()
    _set_meta(
        panel,
        **{
            _META_URL: url.strip(),
            _META_TITLE: title or None,
            _META_UPLOAD: int(meta.get("upload") or 0),
            _META_DOWNLOAD: int(meta.get("download") or 0),
            _META_TOTAL: int(meta.get("total") or 0),
            _META_EXPIRE: int(meta.get("expire") or 0),
            _META_SYNCED: now,
            _META_ERROR: None,
            _META_COUNT: len(parsed_list),
        },
    )
    await db.flush()

    if ping:
        rows = await node.list_outbounds(db, panel.id)
        for row in rows[:40]:
            try:
                await node.ping_outbound(db, row)
            except Exception:  # noqa: BLE001
                pass

    await node.rebuild_xray_config(db, panel)
    try:
        await node.refresh_panel_account_links(db, panel)
    except Exception:  # noqa: BLE001
        logger.debug("refresh links after sub sync failed", exc_info=True)

    info = subscription_info(panel) or {}
    return {
        "ok": True,
        "created": created,
        "updated": updated,
        "removed": removed,
        "total": len(parsed_list),
        "subscription": info,
    }


async def refresh_bound_subscription(
    db: AsyncSession, panel: Panel, *, ping: bool = False
) -> dict[str, Any]:
    info = subscription_info(panel)
    if not info or not info.get("url"):
        raise ValueError("سابی برای این پنل ثبت نشده")
    try:
        return await sync_outbounds_from_subscription(
            db, panel, str(info["url"]), ping=ping
        )
    except Exception as exc:  # noqa: BLE001
        _set_meta(panel, **{_META_ERROR: str(exc)[:500]})
        await db.flush()
        raise
