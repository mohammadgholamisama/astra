"""Rewrite share-link remarks / display names for subscription clients."""

from __future__ import annotations

import base64
import json
import re
import time
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

# Client remarks for REAL configs use ASCII digits — Persian digits break some parsers
_FA_TO_EN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

# Stable fake endpoint — clients show remark, ping fails (intentional for info/promo)
_NOTICE_LINK_BASE = (
    "vless://00000000-0000-0000-0000-000000000001@127.0.0.1:80"
    "?encryption=none&security=none&type=tcp&headerType=none"
)


def to_en_digits(value: object) -> str:
    return str(value).translate(_FA_TO_EN)


def format_bytes_en(n: int) -> str:
    n = max(0, int(n or 0))
    units = [(1024**4, "TB"), (1024**3, "GB"), (1024**2, "MB"), (1024, "KB")]
    for size, label in units:
        if n >= size:
            val = n / size
            text = f"{val:.1f}".rstrip("0").rstrip(".") + f" {label}"
            return text
    return f"{n} B"


def format_bytes_compact(n: int) -> str:
    """Compact remain label for remarks: 3G / 2.5G / 500M / ∞."""
    n = max(0, int(n or 0))
    if n <= 0:
        return "0B"
    units = [(1024**4, "T"), (1024**3, "G"), (1024**2, "M"), (1024, "K")]
    for size, label in units:
        if n >= size:
            val = n / size
            text = f"{val:.1f}".rstrip("0").rstrip(".")
            return f"{text}{label}"
    return f"{n}B"


def remain_badge(used: int, limit: int) -> tuple[str, str]:
    """Return (tag, remain_label) — kept for compatibility."""
    used_i = max(0, int(used or 0))
    limit_i = int(limit or 0)
    if limit_i <= 0:
        return "OK", "∞"
    remain = max(0, limit_i - used_i)
    pct = (remain / limit_i) if limit_i else 0.0
    if pct > 0.5:
        tag = "OK"
    elif pct > 0.2:
        tag = "LOW"
    else:
        tag = "CRIT"
    return tag, format_bytes_compact(remain)


def format_expire_compact(expire: int | None) -> str:
    """Remaining days for status remark: 30d / 0d / ∞."""
    ts = int(expire or 0)
    if ts <= 0:
        return "∞"
    now = int(time.time())
    remain = max(0, ts - now)
    days = remain // 86400
    if days <= 0:
        hours = remain // 3600
        if hours <= 0:
            return "0d"
        return f"{hours}h"
    return f"{days}d"


def sanitize_share_link(link: str) -> str:
    """
    Clean share-link query string for picky V2Ray clients:
    - headerType empty/missing → headerType=none (vless/trojan)
    - drop empty optional params (path=, host=, sid=, fp=, …)
    """
    raw = (link or "").strip()
    if not raw or "://" not in raw:
        return raw

    lower = raw.lower()
    if lower.startswith("vmess://"):
        return raw

    base, hash_sep, frag = raw.partition("#")
    parts = urlsplit(base)
    pairs = parse_qsl(parts.query, keep_blank_values=True)

    cleaned: list[tuple[str, str]] = []
    saw_header_type = False
    for key, val in pairs:
        lk = key.lower()
        v = (val or "").strip()
        if lk == "headertype":
            saw_header_type = True
            cleaned.append((key, v if v else "none"))
            continue
        if not v:
            continue
        cleaned.append((key, v))

    if lower.startswith(("vless://", "trojan://")) and not saw_header_type:
        cleaned.append(("headerType", "none"))

    new_query = urlencode(cleaned, doseq=True)
    new_base = urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, ""))
    if hash_sep:
        return f"{new_base}#{frag}"
    return new_base


def rewrite_link_remark(link: str, remark: str) -> str:
    """Set client-visible name on a share link (fragment / vmess ps)."""
    raw = sanitize_share_link((link or "").strip())
    name = to_en_digits((remark or "").strip())
    if not raw or not name:
        return raw

    lower = raw.lower()
    if lower.startswith("vmess://"):
        try:
            b64 = raw.split("://", 1)[1]
            pad = "=" * (-len(b64) % 4)
            data = json.loads(base64.urlsafe_b64decode(b64 + pad).decode("utf-8", errors="ignore"))
            if not isinstance(data, dict):
                return raw
            data["ps"] = name
            encoded = (
                base64.urlsafe_b64encode(
                    json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                )
                .decode("ascii")
                .rstrip("=")
            )
            return f"vmess://{encoded}"
        except Exception:  # noqa: BLE001
            return raw

    if "://" in raw:
        base, _sep, _frag = raw.partition("#")
        return f"{base}#{quote(name, safe='')}"

    return raw


def link_current_remark(link: str) -> str:
    raw = (link or "").strip()
    if not raw:
        return ""
    lower = raw.lower()
    if lower.startswith("vmess://"):
        try:
            b64 = raw.split("://", 1)[1]
            pad = "=" * (-len(b64) % 4)
            data = json.loads(base64.urlsafe_b64decode(b64 + pad).decode("utf-8", errors="ignore"))
            if isinstance(data, dict):
                return str(data.get("ps") or data.get("remark") or "")
        except Exception:  # noqa: BLE001
            return ""
        return ""
    if "#" in raw:
        return unquote(raw.rsplit("#", 1)[-1])
    return ""


def build_config_remark(
    *,
    name: str,
    config_id: int,
    used: int = 0,
    limit: int = 0,
    brand_tag: str | None = None,
) -> str:
    """Real config title:

    - no tag:  ``#2. 2.5G | name``
    - with tag: ``#2. text 2.5G | name``

    brand_tag comes from settings; empty → omitted entirely (no ``[]``).
    Not used for notice / promo / status rows.
    """
    base = " ".join(to_en_digits(name or "").split()).strip() or "config"
    # Keep ASCII-ish name for picky clients (no emoji in real config titles)
    base = re.sub(r"[^\w\s.\-]+", "", base, flags=re.UNICODE).strip() or "config"
    tag = " ".join((brand_tag or "").split()).strip()
    tag = re.sub(r"[\x00-\x1f\x7f<>\"'`]", "", tag)[:24]
    _badge, remain = remain_badge(used, limit)
    cid = int(config_id)
    if tag:
        return f"#{cid}. {tag} {remain} | {base}"
    return f"#{cid}. {remain} | {base}"


def apply_display_name(
    links: list[dict],
    display_name: str | None,
    *,
    used: int = 0,
    limit: int = 0,
    username: str | None = None,
    brand_tag: str | None = None,
) -> list[dict]:
    """Rename + sanitize REAL configs only — notices / status / promo stay untouched."""
    if not links:
        return links

    preferred = (display_name or "").strip() or (username or "").strip()
    real = [r for r in links if r.get("kind") not in ("notice", "status", "promo")]
    special = [r for r in links if r.get("kind") in ("notice", "status", "promo")]
    out: list[dict] = []
    for i, row in enumerate(real):
        link = sanitize_share_link(row.get("link") or "")
        original = link_current_remark(link).strip()
        if " · " in original and preferred:
            original = ""
        name = preferred or original or "config"
        remark = build_config_remark(
            name=name,
            config_id=i + 1,
            used=used,
            limit=limit,
            brand_tag=brand_tag,
        )
        new_link = rewrite_link_remark(link, remark)
        out.append({**row, "link": new_link, "remark": remark, "config_id": i + 1})
    # Caller orders specials; keep them as-is
    return special + out


def normalize_notices(raw: list | None, *, max_items: int = 10, max_len: int = 200) -> list[str]:
    """Dedupe / trim notice texts for group storage."""
    out: list[str] = []
    seen: set[str] = set()
    for item in raw or []:
        text = " ".join(str(item or "").split()).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text[:max_len])
        if len(out) >= max_items:
            break
    return out


def build_notice_link(text: str) -> str:
    """Fake share link whose remark is the notice — no usable ping."""
    remark = " ".join((text or "").split()).strip()[:200]
    if not remark:
        return ""
    return f"{_NOTICE_LINK_BASE}#{quote(remark, safe='')}"


def build_status_remark(
    *,
    username: str,
    used: int,
    limit: int,
    expire: int | None,
) -> str:
    """First sub line: ``✨ user - 2.5G - 30d ✨`` (updates on every sub refresh)."""
    name = " ".join(to_en_digits(username or "user").split()).strip() or "user"
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)[:32]
    lim = int(limit or 0)
    if lim <= 0:
        hajm = "∞"
    else:
        hajm = format_bytes_compact(max(0, lim - max(0, int(used or 0))))
    t = format_expire_compact(expire)
    # Emoji bookends — display-only; ping intentionally fails
    return f"✨ {name} - {hajm} - {t} ✨"


def build_status_row(
    *,
    username: str,
    used: int,
    limit: int,
    expire: int | None,
) -> dict:
    remark = build_status_remark(
        username=username, used=used, limit=limit, expire=expire
    )
    link = build_notice_link(remark)
    return {
        "link": link,
        "panel": "وضعیت",
        "panel_id": 0,
        "remark": remark,
        "kind": "status",
    }


def build_promo_row(text: str) -> dict | None:
    remark = " ".join(str(text or "").split()).strip()[:200]
    if not remark:
        return None
    link = build_notice_link(remark)
    if not link:
        return None
    return {
        "link": link,
        "panel": "تبلیغات",
        "panel_id": 0,
        "remark": remark,
        "kind": "promo",
    }


def notice_rows_from_groups(groups: list) -> list[dict]:
    """Build prepended notice configs from user's groups (order preserved, unique)."""
    rows: list[dict] = []
    seen: set[str] = set()
    for group in groups or []:
        for raw in getattr(group, "notices", None) or []:
            text = " ".join(str(raw or "").split()).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            link = build_notice_link(text)
            if not link:
                continue
            rows.append(
                {
                    "link": link,
                    "panel": "اطلاع‌رسانی",
                    "panel_id": 0,
                    "remark": text,
                    "kind": "notice",
                }
            )
    return rows
