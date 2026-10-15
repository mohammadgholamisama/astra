"""Cached runtime overrides for public URLs, brand, and optional links.

Env defaults (SUB_BASE_URL / PANEL_BASE_URL / PANEL_NAME) remain the fallback.
Values are stored in DB and cached in-process for hot path (sub links).
"""

from __future__ import annotations

import os
import re
import threading
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.site_setting import SiteSetting

KEY_SUB_BASE_URL = "sub_base_url"
KEY_PANEL_BASE_URL = "panel_base_url"
KEY_BRAND_NAME = "brand_name"
KEY_LINK_CHANNEL = "link_channel"
KEY_LINK_SUPPORT = "link_support"
KEY_LINK_APP = "link_app"
# Text inside [] on real config remarks: ``#2 [TAG] 3G| name``
KEY_CONFIG_BRAND_TAG = "config_brand_tag"
# Promotional fake config (2nd line in every sub when enabled)
KEY_PROMO_CONFIG_ENABLED = "promo_config_enabled"
KEY_PROMO_CONFIG_TEXT = "promo_config_text"

_ALLOWED_KEYS = frozenset(
    {
        KEY_SUB_BASE_URL,
        KEY_PANEL_BASE_URL,
        KEY_BRAND_NAME,
        KEY_LINK_CHANNEL,
        KEY_LINK_SUPPORT,
        KEY_LINK_APP,
        KEY_CONFIG_BRAND_TAG,
        KEY_PROMO_CONFIG_ENABLED,
        KEY_PROMO_CONFIG_TEXT,
    }
)

_lock = threading.RLock()
_cache: dict[str, str] = {}
_loaded = False

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_HTML = re.compile(r"[<>\"'`]")
_DOMAIN_RE = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$"
)


def _env_sub_base() -> str:
    return (get_settings().SUB_BASE_URL or "").rstrip("/")


def _env_panel_base() -> str:
    s = get_settings()
    base = (getattr(s, "PANEL_BASE_URL", None) or "").rstrip("/")
    if base:
        return base
    return (s.SUB_BASE_URL or "").rstrip("/")


def _env_brand() -> str:
    return (get_settings().PANEL_NAME or "Astra").strip() or "Astra"


def normalize_public_base_url(raw: str, *, label: str) -> str:
    """Validate and normalize a public http(s) base URL (panel or sub)."""
    url = (raw or "").strip()
    if not url:
        raise ValueError(f"{label} نمی‌تواند خالی باشد")
    if len(url) > 512:
        raise ValueError(f"{label} خیلی طولانی است")
    if any(ch.isspace() for ch in url):
        raise ValueError(f"{label} نباید فاصله داشته باشد")
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("فقط http یا https مجاز است")
    if not parsed.netloc or parsed.netloc.startswith("."):
        raise ValueError("دامنه نامعتبر است")
    if "@" in parsed.netloc:
        raise ValueError("یوزر/رمز داخل آدرس مجاز نیست")
    path = (parsed.path or "").rstrip("/")
    if path in ("", "/"):
        path = ""
    if ".." in path or path.lower().startswith("/sub"):
        raise ValueError("مسیر پایه نامعتبر است")
    return f"{parsed.scheme}://{parsed.netloc}{path}".rstrip("/")


def normalize_sub_base_url(raw: str) -> str:
    return normalize_public_base_url(raw, label="آدرس دامنه ساب")


def normalize_panel_base_url(raw: str) -> str:
    return normalize_public_base_url(raw, label="آدرس دامنه پنل")


def normalize_brand_name(raw: str) -> str:
    name = _CTRL.sub("", (raw or "").strip())
    name = _HTML.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        raise ValueError("نام برند نمی‌تواند خالی باشد")
    if len(name) > 48:
        raise ValueError("نام برند حداکثر ۴۸ کاراکتر است")
    return name


def normalize_optional_link(raw: str | None, *, label: str) -> str:
    """Optional public URL. Empty clears. Accepts http(s) or @telegram / t.me."""
    url = _CTRL.sub("", (raw or "").strip())
    url = _HTML.sub("", url)
    if not url:
        return ""
    if len(url) > 512:
        raise ValueError(f"{label} خیلی طولانی است")
    if any(ch.isspace() for ch in url):
        raise ValueError(f"{label} نباید فاصله داشته باشد")
    if url.startswith("@"):
        handle = url[1:].lstrip("/")
        if not re.fullmatch(r"[A-Za-z0-9_]{4,64}", handle):
            raise ValueError(f"{label}: آیدی تلگرام نامعتبر است")
        url = f"https://t.me/{handle}"
    elif url.lower().startswith("t.me/") or url.lower().startswith("telegram.me/"):
        url = "https://" + url
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{label}: فقط http یا https مجاز است")
    if not parsed.netloc:
        raise ValueError(f"{label}: آدرس نامعتبر است")
    if "@" in parsed.netloc:
        raise ValueError(f"{label}: یوزر/رمز داخل آدرس مجاز نیست")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path or ''}".rstrip("/") + (
        f"?{parsed.query}" if parsed.query else ""
    )


def extract_hostname(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    host = (urlparse(raw).hostname or "").strip().lower()
    return host


def is_valid_domain(domain: str) -> bool:
    d = (domain or "").strip().lower().rstrip(".")
    if not d or len(d) > 253:
        return False
    return bool(_DOMAIN_RE.match(d))


def _set_cache(data: dict[str, str]) -> None:
    global _loaded
    with _lock:
        _cache.clear()
        _cache.update(data)
        _loaded = True


def invalidate_cache() -> None:
    global _loaded
    with _lock:
        _cache.clear()
        _loaded = False


def get_sub_base_url() -> str:
    with _lock:
        if _loaded and KEY_SUB_BASE_URL in _cache and _cache[KEY_SUB_BASE_URL]:
            return _cache[KEY_SUB_BASE_URL].rstrip("/")
    return _env_sub_base()


def get_panel_base_url() -> str:
    with _lock:
        if _loaded and KEY_PANEL_BASE_URL in _cache and _cache[KEY_PANEL_BASE_URL]:
            return _cache[KEY_PANEL_BASE_URL].rstrip("/")
    return _env_panel_base()


def get_brand_name() -> str:
    with _lock:
        if _loaded and KEY_BRAND_NAME in _cache and _cache[KEY_BRAND_NAME]:
            return _cache[KEY_BRAND_NAME]
    return _env_brand()


def get_link_channel() -> str:
    with _lock:
        if _loaded:
            return (_cache.get(KEY_LINK_CHANNEL) or "").strip()
    return ""


def get_link_support() -> str:
    with _lock:
        if _loaded:
            return (_cache.get(KEY_LINK_SUPPORT) or "").strip()
    return ""


def get_link_app() -> str:
    with _lock:
        if _loaded:
            return (_cache.get(KEY_LINK_APP) or "").strip()
    return ""


def get_public_links() -> dict[str, str]:
    return {
        "channel": get_link_channel(),
        "support": get_link_support(),
        "app": get_link_app(),
    }


def normalize_config_brand_tag(raw: str | None) -> str:
    """Optional short tag shown inside [] on every real config remark."""
    tag = _CTRL.sub("", (raw or "").strip())
    tag = _HTML.sub("", tag)
    tag = re.sub(r"\s+", " ", tag).strip()
    if len(tag) > 24:
        raise ValueError("برچسب کانفیگ حداکثر ۲۴ کاراکتر است")
    return tag


def normalize_promo_text(raw: str | None) -> str:
    text = _CTRL.sub("", (raw or "").strip())
    text = _HTML.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 200:
        raise ValueError("متن تبلیغاتی حداکثر ۲۰۰ کاراکتر است")
    return text


def get_config_brand_tag() -> str:
    with _lock:
        if _loaded:
            return (_cache.get(KEY_CONFIG_BRAND_TAG) or "").strip()
    return ""


def get_promo_config_enabled() -> bool:
    with _lock:
        if _loaded:
            return (_cache.get(KEY_PROMO_CONFIG_ENABLED) or "").strip() in (
                "1",
                "true",
                "yes",
                "on",
            )
    return False


def get_promo_config_text() -> str:
    with _lock:
        if _loaded:
            return (_cache.get(KEY_PROMO_CONFIG_TEXT) or "").strip()
    return ""


def ssl_helper_available() -> bool:
    s = get_settings()
    helper = (getattr(s, "ASTRA_HELPER", None) or "").strip()
    if not helper:
        helper = "/usr/local/bin/astra-helper"
    return bool(helper) and os.path.isfile(helper) and os.access(helper, os.X_OK)


def install_domains() -> tuple[str, str]:
    """Panel / sub domains from env (set by install / x-astra)."""
    s = get_settings()
    panel = (os.environ.get("ASTRA_PANEL_DOMAIN") or getattr(s, "ASTRA_PANEL_DOMAIN", "") or "").strip()
    sub = (os.environ.get("ASTRA_SUB_DOMAIN") or getattr(s, "ASTRA_SUB_DOMAIN", "") or "").strip()
    if not panel:
        panel = extract_hostname(get_panel_base_url())
    if not sub:
        sub = extract_hostname(get_sub_base_url())
    return panel, sub


async def refresh_cache(db: AsyncSession) -> None:
    result = await db.execute(
        select(SiteSetting).where(SiteSetting.key.in_(list(_ALLOWED_KEYS)))
    )
    rows = {r.key: (r.value or "") for r in result.scalars().all()}
    _set_cache(rows)


async def ensure_loaded(db: AsyncSession) -> None:
    with _lock:
        ready = _loaded
    if not ready:
        await refresh_cache(db)


async def read_settings(db: AsyncSession) -> dict:
    await ensure_loaded(db)
    panel_dom, sub_dom = install_domains()
    return {
        "sub_base_url": get_sub_base_url(),
        "panel_base_url": get_panel_base_url(),
        "brand_name": get_brand_name(),
        "link_channel": get_link_channel(),
        "link_support": get_link_support(),
        "link_app": get_link_app(),
        "config_brand_tag": get_config_brand_tag(),
        "promo_config_enabled": get_promo_config_enabled(),
        "promo_config_text": get_promo_config_text(),
        "ssl_available": ssl_helper_available(),
        "panel_domain": panel_dom,
        "sub_domain": sub_dom,
        "defaults": {
            "sub_base_url": _env_sub_base(),
            "panel_base_url": _env_panel_base(),
            "brand_name": _env_brand(),
            "link_channel": "",
            "link_support": "",
            "link_app": "",
            "config_brand_tag": "",
            "promo_config_enabled": "0",
            "promo_config_text": "",
        },
    }


async def _delete_key(db: AsyncSession, key: str) -> None:
    row = await db.get(SiteSetting, key)
    if row is not None:
        await db.delete(row)


async def _upsert_key(db: AsyncSession, key: str, value: str) -> None:
    if not value:
        await _delete_key(db, key)
        return
    row = await db.get(SiteSetting, key)
    if row is None:
        db.add(SiteSetting(key=key, value=value))
    else:
        row.value = value


async def update_settings(
    db: AsyncSession,
    *,
    sub_base_url: str | None = None,
    panel_base_url: str | None = None,
    brand_name: str | None = None,
    link_channel: str | None = None,
    link_support: str | None = None,
    link_app: str | None = None,
    config_brand_tag: str | None = None,
    promo_config_enabled: bool | None = None,
    promo_config_text: str | None = None,
    reset_sub_base_url: bool = False,
    reset_panel_base_url: bool = False,
    reset_brand_name: bool = False,
    clear_link_channel: bool = False,
    clear_link_support: bool = False,
    clear_link_app: bool = False,
    clear_config_brand_tag: bool = False,
    clear_promo_config: bool = False,
) -> dict:
    if reset_sub_base_url:
        await _delete_key(db, KEY_SUB_BASE_URL)
    elif sub_base_url is not None:
        await _upsert_key(db, KEY_SUB_BASE_URL, normalize_sub_base_url(sub_base_url))

    if reset_panel_base_url:
        await _delete_key(db, KEY_PANEL_BASE_URL)
    elif panel_base_url is not None:
        await _upsert_key(db, KEY_PANEL_BASE_URL, normalize_panel_base_url(panel_base_url))

    if reset_brand_name:
        await _delete_key(db, KEY_BRAND_NAME)
    elif brand_name is not None:
        await _upsert_key(db, KEY_BRAND_NAME, normalize_brand_name(brand_name))

    if clear_link_channel:
        await _delete_key(db, KEY_LINK_CHANNEL)
    elif link_channel is not None:
        await _upsert_key(
            db, KEY_LINK_CHANNEL, normalize_optional_link(link_channel, label="لینک کانال")
        )

    if clear_link_support:
        await _delete_key(db, KEY_LINK_SUPPORT)
    elif link_support is not None:
        await _upsert_key(
            db, KEY_LINK_SUPPORT, normalize_optional_link(link_support, label="لینک پشتیبان")
        )

    if clear_link_app:
        await _delete_key(db, KEY_LINK_APP)
    elif link_app is not None:
        await _upsert_key(
            db, KEY_LINK_APP, normalize_optional_link(link_app, label="لینک دانلود برنامه")
        )

    if clear_config_brand_tag:
        await _delete_key(db, KEY_CONFIG_BRAND_TAG)
    elif config_brand_tag is not None:
        await _upsert_key(
            db, KEY_CONFIG_BRAND_TAG, normalize_config_brand_tag(config_brand_tag)
        )

    if clear_promo_config:
        await _delete_key(db, KEY_PROMO_CONFIG_ENABLED)
        await _delete_key(db, KEY_PROMO_CONFIG_TEXT)
    else:
        if promo_config_enabled is not None:
            await _upsert_key(
                db,
                KEY_PROMO_CONFIG_ENABLED,
                "1" if promo_config_enabled else "0",
            )
        if promo_config_text is not None:
            text = normalize_promo_text(promo_config_text)
            if text:
                await _upsert_key(db, KEY_PROMO_CONFIG_TEXT, text)
            else:
                await _delete_key(db, KEY_PROMO_CONFIG_TEXT)

    await db.commit()
    await refresh_cache(db)
    return await read_settings(db)
