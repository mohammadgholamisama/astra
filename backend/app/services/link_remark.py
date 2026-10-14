"""Rewrite share-link remarks / display names for subscription clients."""

from __future__ import annotations

import base64
import json
import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

# Client remarks must use ASCII digits — Persian digits break some V2Ray parsers
_FA_TO_EN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


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


def remain_badge(used: int, limit: int) -> tuple[str, str]:
    """Return (tag, remain_label) for subscription remarks — ASCII-safe (no emoji)."""
    used_i = max(0, int(used or 0))
    limit_i = int(limit or 0)
    if limit_i <= 0:
        return "OK", "unlimited"
    remain = max(0, limit_i - used_i)
    pct = (remain / limit_i) if limit_i else 0.0
    if pct > 0.5:
        tag = "OK"
    elif pct > 0.2:
        tag = "LOW"
    else:
        tag = "CRIT"
    return tag, format_bytes_en(remain)


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
            # Never keep blank query values (path=, host=, fp=, …)
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
) -> str:
    """Client-visible title — ASCII only (no emoji; picky V2Ray parsers)."""
    base = " ".join(to_en_digits(name or "").split()).strip() or "config"
    # Strip leftover emoji / non-ascii symbols from display name
    base = re.sub(r"[^\w\s.\-]+", "", base, flags=re.UNICODE).strip() or "config"
    tag, remain = remain_badge(used, limit)
    return f"{base} [{tag}] {remain} #{int(config_id)}"


def apply_display_name(
    links: list[dict],
    display_name: str | None,
    *,
    used: int = 0,
    limit: int = 0,
    username: str | None = None,
) -> list[dict]:
    """Rename + sanitize real configs for clients/sub page."""
    if not links:
        return links

    preferred = (display_name or "").strip() or (username or "").strip()
    real = [r for r in links if r.get("kind") != "notice"]
    notices = [r for r in links if r.get("kind") == "notice"]
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
        )
        new_link = rewrite_link_remark(link, remark)
        out.append({**row, "link": new_link, "remark": remark, "config_id": i + 1})
    return notices + out


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
    """Fake share link whose remark is the notice — shown first in clients."""
    remark = " ".join((text or "").split()).strip()[:200]
    if not remark:
        return ""
    return (
        "vless://00000000-0000-0000-0000-000000000001@127.0.0.1:80"
        f"?encryption=none&security=none&type=tcp&headerType=none#{quote(remark, safe='')}"
    )


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
