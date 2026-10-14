"""Rewrite share-link remarks / display names for subscription clients."""

from __future__ import annotations

import base64
import json
from urllib.parse import quote, unquote

# Persian digits for client-visible remarks
_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def to_fa_digits(value: object) -> str:
    return str(value).translate(_FA_DIGITS)


def format_bytes_fa(n: int) -> str:
    n = max(0, int(n or 0))
    units = [(1024**4, "TB"), (1024**3, "GB"), (1024**2, "MB"), (1024, "KB")]
    for size, label in units:
        if n >= size:
            val = n / size
            text = f"{val:.1f}".rstrip("0").rstrip(".") + f" {label}"
            return to_fa_digits(text)
    return to_fa_digits(f"{n} B")


def remain_badge(used: int, limit: int) -> tuple[str, str]:
    """Return (emoji, remain_label) for subscription remarks."""
    used_i = max(0, int(used or 0))
    limit_i = int(limit or 0)
    if limit_i <= 0:
        return "♾️", "نامحدود"
    remain = max(0, limit_i - used_i)
    pct = (remain / limit_i) if limit_i else 0.0
    if pct > 0.5:
        emoji = "🟢"
    elif pct > 0.2:
        emoji = "🟡"
    else:
        emoji = "🔴"
    return emoji, format_bytes_fa(remain)


def rewrite_link_remark(link: str, remark: str) -> str:
    """Set client-visible name on a share link (fragment / vmess ps)."""
    raw = (link or "").strip()
    name = (remark or "").strip()
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
            encoded = base64.urlsafe_b64encode(
                json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).decode("ascii").rstrip("=")
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
    """Client-visible title: name + remain emoji/volume + #id — no panel name."""
    base = " ".join((name or "").split()).strip() or "کانفیگ"
    emoji, remain = remain_badge(used, limit)
    cid = to_fa_digits(config_id)
    return f"{base} {emoji} {remain} #{cid}"


def apply_display_name(
    links: list[dict],
    display_name: str | None,
    *,
    used: int = 0,
    limit: int = 0,
    username: str | None = None,
) -> list[dict]:
    """Rename real configs for clients/sub page.

    Format: «اسم 🟢 حجم‌باقی #آیدی» — never includes panel name.
    Skips notice/info rows (kind == \"notice\").
    """
    if not links:
        return links

    preferred = (display_name or "").strip() or (username or "").strip()
    real = [r for r in links if r.get("kind") != "notice"]
    notices = [r for r in links if r.get("kind") == "notice"]
    out: list[dict] = []
    for i, row in enumerate(real):
        link = row.get("link") or ""
        original = link_current_remark(link).strip()
        # Drop stale "name · panel" style if present
        if " · " in original and preferred:
            original = ""
        name = preferred or original or f"کانفیگ"
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
        f"?encryption=none&security=none&type=tcp#{quote(remark, safe='')}"
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
