"""Import / sync a single Astra outbound from an upstream subscription URL.

Each Astra panel keeps **one** outbound. A subscription may contain many
share-links — the admin picks which one to use. Other panels may bind the
same sub and pick a different config; already-used hashes are marked so the
UI can dim them.
"""

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
from sqlalchemy.orm import selectinload

from app.adapters.http_utils import extract_share_links, is_share_link
from app.models.astra_outbound import AstraOutbound
from app.models.panel import Panel, PanelType
from app.services.outbound_uri import (
    ParsedOutbound,
    parse_outbound_lines,
    parse_outbound_uri,
    share_links_from_xray_json,
)

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
_META_SELECTED = "outbound_sub_selected_hash"

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
        "selected_hash": str(meta.get(_META_SELECTED) or "").strip() or None,
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
            _META_SELECTED: None,
        },
    )


def clear_selected_hash(panel: Panel) -> None:
    """Keep the bound sub URL/usage, but free the picked config for other panels."""
    _set_meta(panel, **{_META_SELECTED: None})


def _decode_header_title(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    low = text.lower()
    if low.startswith("base64:"):
        b64 = text.split(":", 1)[1].strip()
        try:
            pad = "=" * (-len(b64) % 4)
            return base64.b64decode(b64 + pad).decode("utf-8", "ignore").strip()
        except Exception:  # noqa: BLE001
            return b64
    try:
        if re.fullmatch(r"[A-Za-z0-9+/=\s]+", text) and len(text) >= 8:
            pad = "=" * (-len(text) % 4)
            dec = base64.b64decode(text + pad).decode("utf-8", "ignore").strip()
            if dec and (any(ord(c) > 127 for c in dec) or " " in dec):
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


def _is_real_config_remark(remark: str) -> bool:
    """Skip status / promo / notice fake rows from Astra-style subs."""
    t = (remark or "").strip()
    if not t:
        return True
    if t.startswith("✨") or "✨" in t[:3]:
        return False
    # Numbered real configs: #1. …
    if re.match(r"^#\d+\.", t):
        return True
    # Promo / notice lines rarely look like #N.
    if "|" in t and not re.search(r"\d+(?:\.\d+)?[KMGT]?B?\b|\b∞\b", t):
        # soft: keep unless clearly non-config slogans without host-ish content
        pass
    return True


# Panels often switch response format by User-Agent:
# v2rayNG → Xray JSON array, v2rayN/Shadowrocket → base64 share-links, Clash → YAML.
_SUB_USER_AGENTS = (
    "v2rayN/6.23",
    "shadowrocket",
    "SagerNet/2.0.2",
    "v2rayNG/1.8.5",
    "ClashMetaForAndroid/2.11.1",
)


def _links_from_sub_body(body: str) -> list[str]:
    text = (body or "").strip()
    if not text:
        return []
    links = extract_share_links(text)
    if not links and is_share_link(text.splitlines()[0] if text else ""):
        links = [text.splitlines()[0].strip()]
    if not links:
        links = share_links_from_xray_json(text)
    return links


async def fetch_subscription(url: str) -> tuple[list[str], dict[str, Any]]:
    """Return (share_links, meta) from a subscription URL."""
    u = (url or "").strip()
    if not u:
        raise ValueError("آدرس ساب خالی است")
    if not (u.startswith("http://") or u.startswith("https://")):
        raise ValueError("آدرس ساب باید با http یا https شروع شود")

    timeout = httpx.Timeout(25.0, connect=8.0)
    base_headers = {
        "Accept": "text/plain,*/*",
        "Cache-Control": "no-cache",
    }

    body = ""
    rh: dict[str, str] = {}
    links: list[str] = []
    last_status = 0

    async with httpx.AsyncClient(
        timeout=timeout,
        verify=False,
        follow_redirects=True,
    ) as client:
        for ua in _SUB_USER_AGENTS:
            try:
                resp = await client.get(
                    u, headers={**base_headers, "User-Agent": ua}
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("sub fetch ua=%s failed: %s", ua, exc)
                continue
            last_status = int(resp.status_code or 0)
            if last_status >= 400:
                continue
            cand = (resp.text or "").strip()
            if not cand:
                continue
            # Prefer the first UA that yields real share-links
            found = _links_from_sub_body(cand)
            if found:
                body = cand
                rh = {k.lower(): v for k, v in resp.headers.items()}
                links = found
                break
            # Keep first non-empty body as fallback for meta / JSON retry
            if not body:
                body = cand
                rh = {k.lower(): v for k, v in resp.headers.items()}

    if last_status >= 400 and not body:
        raise ValueError(f"دریافت ساب ناموفق بود ({last_status})")
    if not links:
        links = _links_from_sub_body(body)
    if not links:
        raise ValueError("داخل این ساب لینک کانفیگ معتبری پیدا نشد")

    title = _decode_header_title(
        rh.get("profile-title") or rh.get("content-disposition") or ""
    )
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

    return links[:_MAX_LINKS], {
        "title": title or None,
        "upload": info["upload"],
        "download": info["download"],
        "total": info["total"],
        "expire": info["expire"],
    }


def _parse_link_safe(link: str) -> ParsedOutbound | None:
    try:
        return parse_outbound_uri(link)
    except ValueError:
        return None


async def _used_configs_by_panel(
    db: AsyncSession, *, exclude_panel_id: int | None = None
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return (by_hash, by_fingerprint) maps for configs already bound on Astra panels."""
    q = (
        select(AstraOutbound)
        .options(selectinload(AstraOutbound.panel))
        .join(Panel, Panel.id == AstraOutbound.panel_id)
        .where(Panel.type == PanelType.astra)
    )
    if exclude_panel_id is not None:
        q = q.where(AstraOutbound.panel_id != int(exclude_panel_id))
    rows = list((await db.execute(q)).scalars().all())
    by_hash: dict[str, dict[str, Any]] = {}
    by_fp: dict[str, dict[str, Any]] = {}
    for row in rows:
        panel = getattr(row, "panel", None)
        info = {
            "panel_id": int(row.panel_id),
            "panel_name": str(getattr(panel, "name", None) or f"پنل #{row.panel_id}"),
            "outbound_id": int(row.id),
            "remark": str(row.remark or ""),
        }
        h = str(row.uri_hash or "").strip()
        if h:
            by_hash[h] = info
        fp = _fingerprint_from_row(row)
        if fp and not fp.endswith("|0|"):
            by_fp[fp] = info
    return by_hash, by_fp


def _config_item(
    p: ParsedOutbound,
    *,
    index: int,
    used_by_hash: dict[str, dict[str, Any]] | None = None,
    used_by_fp: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    hit = (used_by_hash or {}).get(p.uri_hash) or (used_by_fp or {}).get(
        _fingerprint_of(p)
    )
    return {
        "index": index,
        "uri": p.uri,
        "uri_hash": p.uri_hash,
        "remark": (p.remark or "").strip() or f"{p.protocol}-{p.host}",
        "protocol": p.protocol,
        "host": p.host,
        "port": int(p.port or 0),
        "used": bool(hit),
        "used_by_panel_id": int(hit["panel_id"]) if hit else None,
        "used_by_panel_name": str(hit["panel_name"]) if hit else None,
    }


async def preview_subscription(
    db: AsyncSession,
    url: str,
    *,
    exclude_panel_id: int | None = None,
) -> dict[str, Any]:
    """Fetch sub freshly and list pickable configs (no DB writes)."""
    links, meta = await fetch_subscription(url)
    used_by_hash, used_by_fp = await _used_configs_by_panel(
        db, exclude_panel_id=exclude_panel_id
    )
    configs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in links:
        p = _parse_link_safe(link)
        if p is None:
            continue
        if not _is_real_config_remark(p.remark):
            # Still allow if it has a normal host (some status rows use 127.0.0.1)
            if (p.host or "").startswith("127.") or p.host in ("0.0.0.0", "localhost"):
                continue
        if p.uri_hash in seen:
            continue
        seen.add(p.uri_hash)
        configs.append(
            _config_item(
                p,
                index=len(configs) + 1,
                used_by_hash=used_by_hash,
                used_by_fp=used_by_fp,
            )
        )

    if not configs:
        # Fallback: parse via bulk helper (may include borderline rows)
        try:
            parsed_list = parse_outbound_lines("\n".join(links))
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        for p in parsed_list:
            if p.uri_hash in seen:
                continue
            seen.add(p.uri_hash)
            configs.append(
                _config_item(
                    p,
                    index=len(configs) + 1,
                    used_by_hash=used_by_hash,
                    used_by_fp=used_by_fp,
                )
            )

    if not configs:
        raise ValueError("داخل این ساب کانفیگ قابل‌انتخابی پیدا نشد")

    return {
        "url": url.strip(),
        "title": meta.get("title"),
        "upload": int(meta.get("upload") or 0),
        "download": int(meta.get("download") or 0),
        "total": int(meta.get("total") or 0),
        "expire": int(meta.get("expire") or 0),
        "count": len(configs),
        "configs": configs,
    }


async def _apply_meta(
    panel: Panel,
    *,
    url: str,
    meta: dict[str, Any],
    count: int,
    selected_hash: str | None,
    error: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _set_meta(
        panel,
        **{
            _META_URL: url.strip(),
            _META_TITLE: (meta.get("title") or None),
            _META_UPLOAD: int(meta.get("upload") or 0),
            _META_DOWNLOAD: int(meta.get("download") or 0),
            _META_TOTAL: int(meta.get("total") or 0),
            _META_EXPIRE: int(meta.get("expire") or 0),
            _META_SYNCED: now,
            _META_ERROR: error,
            _META_COUNT: int(count),
            _META_SELECTED: (selected_hash or None),
        },
    )


def _fingerprint_of(p: ParsedOutbound) -> str:
    return p.fingerprint


def _fingerprint_from_row(row: AstraOutbound) -> str:
    host = str(row.host or "").strip().lower().strip("[]")
    ident = ""
    parsed = row.parsed if isinstance(row.parsed, dict) else {}
    ident = str(
        parsed.get("user")
        or parsed.get("uuid")
        or parsed.get("id")
        or parsed.get("password")
        or ""
    ).strip().lower()
    if not ident:
        # Best-effort from URI
        try:
            p = parse_outbound_uri(str(row.uri or ""))
            return p.fingerprint
        except Exception:  # noqa: BLE001
            pass
    return f"{str(row.protocol or '').lower()}|{host}|{int(row.port or 0)}|{ident}"


def _pick_from_links(
    links: list[str],
    *,
    uri_hash: str | None = None,
    fingerprint: str | None = None,
    prefer_uri: str | None = None,
) -> tuple[ParsedOutbound | None, list[ParsedOutbound]]:
    """Match by exact hash, then fingerprint; optionally seed from prefer_uri."""
    parsed_list: list[ParsedOutbound] = []
    by_hash: dict[str, ParsedOutbound] = {}
    by_fp: dict[str, ParsedOutbound] = {}
    for link in links:
        p = _parse_link_safe(link)
        if p is None:
            continue
        parsed_list.append(p)
        by_hash[p.uri_hash.lower()] = p
        by_fp[_fingerprint_of(p)] = p

    picked: ParsedOutbound | None = None
    want = (uri_hash or "").strip().lower()
    if want and want in by_hash:
        picked = by_hash[want]
    if picked is None and fingerprint and fingerprint in by_fp:
        picked = by_fp[fingerprint]
    if picked is None and prefer_uri:
        seed = _parse_link_safe(prefer_uri)
        if seed is not None:
            picked = by_fp.get(_fingerprint_of(seed)) or seed
    return picked, parsed_list


async def bind_outbound_from_subscription(
    db: AsyncSession,
    panel: Panel,
    url: str,
    *,
    uri_hash: str,
    uri: str | None = None,
    ping: bool = True,
) -> dict[str, Any]:
    """Attach one chosen share-link as this panel's outbound (with ping)."""
    from app.services import astra_node as node

    want = (uri_hash or "").strip().lower()
    prefer = (uri or "").strip() or None
    if not want and not prefer:
        raise ValueError("کانفیگ انتخاب نشده")

    # Fast path: trust the URI from the preview list, then refresh meta from sub
    seed = _parse_link_safe(prefer) if prefer else None
    links, meta = await fetch_subscription(url)
    picked, parsed_list = _pick_from_links(
        links,
        uri_hash=want or None,
        fingerprint=_fingerprint_of(seed) if seed else None,
        prefer_uri=prefer,
    )
    if picked is None:
        raise ValueError("کانفیگ انتخاب‌شده داخل این ساب پیدا نشد — دوباره لیست را بگیرید")

    used_by_hash, used_by_fp = await _used_configs_by_panel(
        db, exclude_panel_id=int(panel.id)
    )
    conflict = used_by_hash.get(picked.uri_hash) or used_by_fp.get(
        _fingerprint_of(picked)
    )
    if conflict:
        raise ValueError(
            f"این کانفیگ الان روی پنل «{conflict['panel_name']}» است — "
            "اول آنجا حذف کنید یا کانفیگ دیگری بردارید"
        )

    existing = await node.list_outbounds(db, panel.id)
    created = 0
    updated = 0
    removed = 0
    remark = (picked.remark or "").strip()[:255] or f"{picked.protocol}-{picked.host}"
    picked_fp = _fingerprint_of(picked)

    keep: AstraOutbound | None = None
    for row in existing:
        same = (
            str(row.uri_hash).lower() == picked.uri_hash.lower()
            or _fingerprint_from_row(row) == picked_fp
        )
        if same and keep is None:
            keep = row
        else:
            await db.delete(row)
            removed += 1

    if keep is None:
        keep = AstraOutbound(
            panel_id=panel.id,
            uri=picked.uri,
            uri_hash=picked.uri_hash,
            remark=remark,
            protocol=picked.protocol,
            host=picked.host,
            port=picked.port,
            parsed=picked.raw,
            is_enabled=True,
            sort_order=1,
        )
        db.add(keep)
        created = 1
    else:
        keep.uri = picked.uri
        keep.uri_hash = picked.uri_hash
        keep.remark = remark
        keep.protocol = picked.protocol
        keep.host = picked.host
        keep.port = picked.port
        keep.parsed = picked.raw
        keep.sort_order = 1
        keep.is_enabled = True
        updated = 1

    await _apply_meta(
        panel,
        url=url.strip(),
        meta=meta,
        count=len(parsed_list) or 1,
        selected_hash=picked.uri_hash,
        error=None,
    )
    await db.flush()

    # Rebuild first so the node is ready, then measure ping for the UI
    await node.rebuild_xray_config(db, panel)
    if ping:
        try:
            await node.ping_outbound(db, keep)
        except Exception:  # noqa: BLE001
            pass
    try:
        await node.refresh_panel_account_links(db, panel)
    except Exception:  # noqa: BLE001
        logger.debug("refresh links after sub bind failed", exc_info=True)

    info = subscription_info(panel) or {}
    return {
        "ok": True,
        "created": created,
        "updated": updated,
        "removed": removed,
        "total": 1,
        "subscription": info,
    }


async def refresh_bound_subscription(
    db: AsyncSession, panel: Panel, *, ping: bool = False
) -> dict[str, Any]:
    """Re-fetch bound sub for fresh title/usage; keep the selected outbound in sync."""
    from app.services import astra_node as node

    info = subscription_info(panel)
    if not info or not info.get("url"):
        raise ValueError("سابی برای این پنل ثبت نشده")
    url = str(info["url"])
    selected = str(info.get("selected_hash") or "").strip()

    try:
        links, meta = await fetch_subscription(url)
    except Exception as exc:  # noqa: BLE001
        _set_meta(panel, **{_META_ERROR: str(exc)[:500]})
        await db.flush()
        raise

    existing = await node.list_outbounds(db, panel.id)
    if not selected and existing:
        selected = str(existing[0].uri_hash or "").strip()

    row_fp = _fingerprint_from_row(existing[0]) if existing else None
    picked, parsed_list = _pick_from_links(
        links,
        uri_hash=selected or None,
        fingerprint=row_fp,
        prefer_uri=str(existing[0].uri) if existing else None,
    )

    updated = 0
    if picked is not None:
        remark = (picked.remark or "").strip()[:255] or f"{picked.protocol}-{picked.host}"
        selected = picked.uri_hash
        if existing:
            row = existing[0]
            for extra in existing[1:]:
                await db.delete(extra)
            row.uri = picked.uri
            row.uri_hash = picked.uri_hash
            row.remark = remark
            row.protocol = picked.protocol
            row.host = picked.host
            row.port = picked.port
            row.parsed = picked.raw
            row.sort_order = 1
            updated = 1
            if ping:
                try:
                    await node.ping_outbound(db, row)
                except Exception:  # noqa: BLE001
                    pass
        else:
            row = AstraOutbound(
                panel_id=panel.id,
                uri=picked.uri,
                uri_hash=picked.uri_hash,
                remark=remark,
                protocol=picked.protocol,
                host=picked.host,
                port=picked.port,
                parsed=picked.raw,
                is_enabled=True,
                sort_order=1,
            )
            db.add(row)
            updated = 1

    # Only warn when we have a selection but nothing matches AND no outbound left
    err: str | None = None
    if picked is None and selected and not existing:
        err = "کانفیگ انتخاب‌شده دیگر در ساب نیست — یک کانفیگ دیگر انتخاب کنید"
    # If outbound still exists but encoding drifted, keep it and clear stale error

    await _apply_meta(
        panel,
        url=url,
        meta=meta,
        count=len(parsed_list),
        selected_hash=(picked.uri_hash if picked else (selected or None)),
        error=err,
    )
    await db.flush()

    if updated:
        await node.rebuild_xray_config(db, panel)
        try:
            await node.refresh_panel_account_links(db, panel)
        except Exception:  # noqa: BLE001
            logger.debug("refresh links after sub meta sync failed", exc_info=True)

    fresh = subscription_info(panel) or {}
    return {
        "ok": True,
        "created": 0,
        "updated": updated,
        "removed": 0,
        "total": 1 if existing or updated else 0,
        "subscription": fresh,
    }


# Back-compat name used by older callers / tests
async def sync_outbounds_from_subscription(
    db: AsyncSession,
    panel: Panel,
    url: str,
    *,
    ping: bool = False,
    uri_hash: str | None = None,
) -> dict[str, Any]:
    if not uri_hash:
        raise ValueError("یک کانفیگ از لیست ساب انتخاب کنید")
    return await bind_outbound_from_subscription(
        db, panel, url, uri_hash=uri_hash, ping=ping
    )
