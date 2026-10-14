"""Rewrite share-link remarks / display names for subscription clients."""

from __future__ import annotations

import base64
import json
from urllib.parse import quote, unquote


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


def apply_display_name(links: list[dict], display_name: str | None) -> list[dict]:
    """Attach renamed links when user has a custom display_name.

    Skips notice/info rows (kind == \"notice\") so messages stay intact.
    """
    name = (display_name or "").strip()
    if not name or not links:
        return links

    real = [r for r in links if r.get("kind") != "notice"]
    notices = [r for r in links if r.get("kind") == "notice"]
    total = len(real)
    out: list[dict] = []
    for i, row in enumerate(real):
        link = row.get("link") or ""
        if total == 1:
            remark = name
        else:
            panel = (row.get("panel") or "").strip()
            remark = f"{name} · {panel}" if panel else f"{name} · {i + 1}"
        new_link = rewrite_link_remark(link, remark)
        out.append({**row, "link": new_link, "remark": remark})
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
    # Non-routable localhost vless — clients list the #remark as the config name
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
