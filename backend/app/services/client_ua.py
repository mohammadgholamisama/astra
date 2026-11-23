"""Parse User-Agent + Client Hints into brand / model / OS / browser."""

from __future__ import annotations

import re
from typing import Any

_BROWSER_UA = re.compile(
    r"chrome|crios|firefox|fxios|safari|edg(?:e|a)?|opr|opera|samsungbrowser|"
    r"yabrowser|miuibrowser|ucbrowser|brave",
    re.I,
)

_CLIENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("v2rayNG", re.compile(r"v2rayng", re.I)),
    ("v2rayN", re.compile(r"v2rayn(?!g)", re.I)),
    ("Streisand", re.compile(r"streisand", re.I)),
    ("HiddifyNext", re.compile(r"hiddifynext|hiddify.?next", re.I)),
    ("Hiddify", re.compile(r"hiddify", re.I)),
    ("ClashMeta", re.compile(r"clash.?meta|mihomo", re.I)),
    ("Clash", re.compile(r"clash", re.I)),
    ("Shadowrocket", re.compile(r"shadowrocket", re.I)),
    ("Quantumult", re.compile(r"quantumult", re.I)),
    ("Surge", re.compile(r"surge", re.I)),
    ("NekoBox", re.compile(r"nekobox|nekoray", re.I)),
    ("v2box", re.compile(r"v2box", re.I)),
    ("Happ", re.compile(r"(?:^|[^\w])happ/", re.I)),
    ("v2rayTun", re.compile(r"v2raytun", re.I)),
    ("FairVPN", re.compile(r"fairvpn", re.I)),
    ("FoXray", re.compile(r"foxray", re.I)),
    ("Stash", re.compile(r"stash", re.I)),
    ("SingBox", re.compile(r"sing-?box|sfa|sfm", re.I)),
    ("OkHttp", re.compile(r"okhttp", re.I)),
    ("AstraPing", re.compile(r"astraping", re.I)),
]

KNOWN_APP_NAMES = frozenset(name for name, _ in _CLIENT_PATTERNS)

_BRAND_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("iPhone", re.compile(r"iphone", re.I)),
    ("iPad", re.compile(r"ipad", re.I)),
    ("Samsung", re.compile(r"samsung|\bsm-[a-z0-9]+", re.I)),
    ("Xiaomi", re.compile(r"xiaomi|\bredmi\b|\bpoco\b|\bmi\s?\d|\bmiui", re.I)),
    ("Huawei", re.compile(r"huawei|\balphabet|huaweih|hwa-|lya-|ana-|els-", re.I)),
    ("Honor", re.compile(r"\bhonor\b|\bhry-|\bnth-|\bany-", re.I)),
    ("OnePlus", re.compile(r"oneplus|\bop\d{2}[a-z]?\b|\bne\d{4}", re.I)),
    ("OPPO", re.compile(r"\boppo\b|\bcph\d+|\bpdcm\d+", re.I)),
    ("vivo", re.compile(r"\bvivo\b|\bv\d{4}[a-z]?", re.I)),
    ("Realme", re.compile(r"realme|\brm[x-]?\d+", re.I)),
    ("Nokia", re.compile(r"nokia", re.I)),
    ("Google Pixel", re.compile(r"\bpixel\b", re.I)),
    ("Sony", re.compile(r"sony|xperia", re.I)),
    ("LG", re.compile(r"\blg[-\_\s]", re.I)),
    ("Motorola", re.compile(r"motorola|\bmoto\b", re.I)),
    ("Nothing", re.compile(r"\bnothing\b|ainet", re.I)),
    ("Tecno", re.compile(r"\btecno\b", re.I)),
    ("Infinix", re.compile(r"\binfinix\b", re.I)),
    ("Asus", re.compile(r"\basus\b|zenfone", re.I)),
    ("Lenovo", re.compile(r"lenovo", re.I)),
    ("HTC", re.compile(r"\bhtc\b", re.I)),
    ("ZTE", re.compile(r"\bzte\b", re.I)),
]

_MODEL_JUNK = frozenset(
    {
        "",
        "linux",
        "compatible",
        "null",
        "n",
        "mobile",
        "k",
        "tablet",
        "tv",
        "unknown",
        "android",
        "wv",
        "gecko",
        "u",
        "like",
        "smart-tv",
    }
)

_SAMSUNG_MAP = {
    "sm-s918": "Galaxy S23 Ultra",
    "sm-s916": "Galaxy S23+",
    "sm-s911": "Galaxy S23",
    "sm-s928": "Galaxy S24 Ultra",
    "sm-s926": "Galaxy S24+",
    "sm-s921": "Galaxy S24",
    "sm-s938": "Galaxy S25 Ultra",
    "sm-s936": "Galaxy S25+",
    "sm-s931": "Galaxy S25",
    "sm-a546": "Galaxy A54",
    "sm-a556": "Galaxy A55",
    "sm-a356": "Galaxy A35",
    "sm-a256": "Galaxy A25",
    "sm-a156": "Galaxy A15",
    "sm-a057": "Galaxy A05",
    "sm-a145": "Galaxy A14",
    "sm-m346": "Galaxy M34",
    "sm-f946": "Galaxy Z Fold5",
    "sm-f731": "Galaxy Z Flip5",
    "sm-f956": "Galaxy Z Fold6",
    "sm-f741": "Galaxy Z Flip6",
    "sm-g991": "Galaxy S21",
    "sm-g996": "Galaxy S21+",
    "sm-g998": "Galaxy S21 Ultra",
    "sm-s901": "Galaxy S22",
    "sm-s906": "Galaxy S22+",
    "sm-s908": "Galaxy S22 Ultra",
    "sm-n986": "Galaxy Note20 Ultra",
    "sm-g781": "Galaxy S20 FE",
}

# Darwin major → approximate Apple OS major (iOS/macOS)
_DARWIN_MAP: dict[int, tuple[int, int]] = {
    14: (11, 11),
    15: (12, 12),
    16: (13, 13),
    17: (14, 14),
    18: (15, 15),
    26: (26, 26),
    27: (27, 27),
    28: (28, 28),
}

ACCEPT_CH_HEADERS: dict[str, str] = {
    "Accept-CH": (
        "Sec-CH-UA-Model, Sec-CH-UA-Platform, Sec-CH-UA-Platform-Version, "
        "Sec-CH-UA-Mobile, Sec-CH-UA-Arch, Sec-CH-UA-Bitness, Sec-CH-UA-Full-Version-List"
    ),
    "Critical-CH": "Sec-CH-UA-Model, Sec-CH-UA-Platform, Sec-CH-UA-Platform-Version",
    "Permissions-Policy": (
        "ch-ua-model=(self), ch-ua-platform=(self), "
        "ch-ua-platform-version=(self), ch-ua-mobile=(self)"
    ),
    "Vary": "Sec-CH-UA-Model, Sec-CH-UA-Platform, Sec-CH-UA-Platform-Version, User-Agent",
}


def _h(headers: dict[str, str] | None, name: str) -> str:
    if not headers:
        return ""
    want = name.lower()
    for k, v in headers.items():
        if k.lower() == want:
            return (v or "").strip().strip('"')
    return ""


def extract_client_hints(headers: dict[str, str] | None) -> dict[str, str]:
    """Read low-entropy Client Hints when the browser sends them."""
    out = {
        "model": _h(headers, "sec-ch-ua-model"),
        "platform": _h(headers, "sec-ch-ua-platform"),
        "platform_version": _h(headers, "sec-ch-ua-platform-version"),
        "mobile": _h(headers, "sec-ch-ua-mobile"),
        "arch": _h(headers, "sec-ch-ua-arch"),
        "bitness": _h(headers, "sec-ch-ua-bitness"),
        "ua_full": _h(headers, "sec-ch-ua-full-version-list") or _h(headers, "sec-ch-ua"),
    }
    return {k: v for k, v in out.items() if v}


def parse_browser_name(user_agent: str, hints: dict[str, str] | None = None) -> str | None:
    ua = user_agent or ""
    low = ua.lower()
    full = ((hints or {}).get("ua_full") or "").lower()
    fl = full or low

    if "google chrome" in fl or '"chrome"' in fl:
        return "Chrome"
    if "microsoft edge" in fl or "edg/" in low or "edga/" in low:
        return "Edge"
    if "firefox" in fl or "fxios" in low:
        return "Firefox"
    if "samsungbrowser" in low or ("samsung" in fl and "browser" in fl):
        return "Samsung Internet"
    if "miuibrowser" in low:
        return "Mi Browser"
    if "yabrowser" in low:
        return "Yandex"
    if "ucbrowser" in low:
        return "UC Browser"
    if "opr/" in low or "opera" in low:
        return "Opera"
    if "crios/" in low or "chrome/" in low or "chromium" in low:
        return "Chrome"
    if "edg" in low:
        return "Edge"
    if "safari/" in low and "chrome" not in low and "crios" not in low:
        return "Safari"
    if _BROWSER_UA.search(ua):
        return "مرورگر"
    return None


def _clean_model_token(raw: str) -> str | None:
    s = re.sub(r"\s+", " ", (raw or "").strip())
    s = s.strip("[]()\"'")
    s = re.sub(r"\s+Build/.*$", "", s, flags=re.I).strip()
    if not s or s.lower() in _MODEL_JUNK:
        return None
    return s


def _marketing_name(model: str) -> str | None:
    m = (model or "").lower()
    sm = re.search(r"(sm-[a-z0-9]{3,})", m)
    if sm:
        key = sm.group(1)
        for prefix, name in _SAMSUNG_MAP.items():
            if key.startswith(prefix):
                return name
    if "pixel" in m:
        return model
    return None


def parse_device_model(user_agent: str, hints: dict[str, str] | None = None) -> str | None:
    """Best-effort phone/tablet model. None if UA reduction hid it."""
    hint_model = (hints or {}).get("model") or ""
    if hint_model:
        nice = _marketing_name(hint_model) or _clean_model_token(hint_model)
        if nice:
            return nice
    ua = user_agent or ""
    m = re.search(r"Android[^;]*;\s*([^;)]+?)(?:\s+Build/|\s*;|\))", ua, re.I)
    if m:
        token = _clean_model_token(m.group(1))
        if token:
            return _marketing_name(token) or token
    return None


def parse_device_brand(user_agent: str, hints: dict[str, str] | None = None) -> str:
    ua = user_agent or ""
    model = parse_device_model(ua, hints) or ""
    blob = f"{ua} {model} {(hints or {}).get('platform', '')}"
    for name, pat in _BRAND_PATTERNS:
        if pat.search(blob):
            return name
    low = ua.lower()
    plat = ((hints or {}).get("platform") or "").lower()
    if "windows" in plat or "windows" in low:
        return "Windows PC"
    if "mac os" in plat or "macintosh" in low:
        return "Mac"
    if "android" in plat or "android" in low:
        return "Android"
    if "iphone" in low or plat == "ios":
        return "iPhone"
    if "ipad" in low:
        return "iPad"
    if "linux" in low:
        return "Linux PC"
    if re.search(r"v2rayng|nekobox|hiddifynext|hiddify|okhttp", low):
        return "Android"
    if re.search(r"v2rayn(?!g)", low):
        return "Windows PC"
    if "cfnetwork" in low or "darwin/" in low:
        if re.search(r"streisand|shadowrocket|quantumult|foxray|\bstash\b", low):
            return "iPhone"
        if re.search(r"\bv2box/", low):
            return "iPhone"
    return "سایر"


def _ver_join(major: str, rest: str | None = None) -> str:
    parts = [major]
    if rest:
        p = rest.strip(".")
        if p:
            parts.append(p)
    return ".".join(parts)


def _plausible_android_major(n: int) -> bool:
    return 5 <= n <= 20


def _plausible_apple_major(n: int) -> bool:
    return 10 <= n <= 30


def _bitness_suffix(hints: dict[str, str], low_ua: str) -> str:
    bit = (hints.get("bitness") or "").strip()
    arch = (hints.get("arch") or "").lower()
    if bit == "64" or arch in ("x86_64", "arm64"):
        return " · 64-bit"
    if bit == "32":
        return " · 32-bit"
    if re.search(r"win64|x64|wow64|x86_64|aarch64|arm64", low_ua):
        return " · 64-bit"
    return ""


def _os_from_darwin(low: str) -> str | None:
    m = re.search(r"darwin/(\d+)(?:\.(\d+))?", low)
    if not m:
        return None
    major = int(m.group(1))
    mapped = _DARWIN_MAP.get(major)
    if "iphone" in low or "ios" in low or re.search(r"\bmobile/", low):
        ver = mapped[0] if mapped else major
        return f"iOS {ver}"
    if "ipad" in low:
        ver = mapped[0] if mapped else major
        return f"iPadOS {ver}"
    if "mac" in low or "macintosh" in low or "cfnetwork" in low:
        ver = mapped[1] if mapped else major
        return f"macOS {ver}"
    return None


def parse_client_os(user_agent: str, hints: dict[str, str] | None = None) -> str:
    """Human OS string: Android 14, iOS 17.4, Windows 11, macOS 14.2, …"""
    raw = user_agent or ""
    ua = raw
    low = ua.lower()
    hints = hints or {}
    plat = (hints.get("platform") or "").lower()
    pver = (hints.get("platform_version") or "").strip().strip('"')

    if plat == "android" and pver:
        major = pver.split(".")[0]
        try:
            if _plausible_android_major(int(major)):
                return f"Android {_ver_join(major)}"
        except ValueError:
            pass
    if plat in ("ios", "ipados", "ipad") and pver:
        parts = [p for p in pver.replace("_", ".").split(".") if p]
        if parts:
            try:
                if _plausible_apple_major(int(parts[0])):
                    label = "iPadOS" if plat in ("ipados", "ipad") else "iOS"
                    return f"{label} {_ver_join(parts[0], '.'.join(parts[1:2]) or None)}"
            except ValueError:
                pass
    if plat == "windows" and pver:
        try:
            major = int(pver.split(".")[0])
            if major >= 13:
                return "Windows 11" + _bitness_suffix(hints, low)
            if major >= 10:
                return "Windows 10" + _bitness_suffix(hints, low)
        except ValueError:
            pass
    if plat in ("mac os", "macos", "mac os x") and pver:
        parts = [p for p in pver.replace("_", ".").split(".") if p]
        if parts:
            return f"macOS {_ver_join(parts[0], '.'.join(parts[1:2]) or None)}"

    if re.search(r"android\s+10;\s*k\b", low):
        return "Android"

    m = re.search(
        r"\bios[/\s:_-]+([0-9]{1,2})(?:[._]([0-9]{1,2}))?(?:[._]([0-9]{1,2}))?\b",
        low,
    )
    if m and _plausible_apple_major(int(m.group(1))):
        return f"iOS {_ver_join(m.group(1), m.group(2))}"

    m = re.search(
        r"\bipados[/\s:_-]+([0-9]{1,2})(?:[._]([0-9]{1,2}))?(?:[._]([0-9]{1,2}))?\b",
        low,
    )
    if m and _plausible_apple_major(int(m.group(1))):
        return f"iPadOS {_ver_join(m.group(1), m.group(2))}"

    m = re.search(
        r"(?:cpu (?:iphone )?os|iphone os)\s*([0-9]{1,2})(?:[._]([0-9]{1,2}))?(?:[._]([0-9]{1,2}))?",
        low,
    )
    if m and _plausible_apple_major(int(m.group(1))):
        return f"iOS {_ver_join(m.group(1), m.group(2))}"

    m = re.search(r"\bandroid[/\s_-]+([0-9]{1,2})(?:\.([0-9]{1,2}))?\b", low)
    if m and _plausible_android_major(int(m.group(1))):
        return f"Android {_ver_join(m.group(1), m.group(2))}"

    darwin = _os_from_darwin(low)
    if darwin:
        return darwin

    if "windows nt 10" in low or "windows nt 11" in low:
        return "Windows 10/11" + _bitness_suffix(hints, low)
    if "windows nt 6.3" in low:
        return "Windows 8.1"
    if "windows nt 6.2" in low:
        return "Windows 8"
    if "windows nt 6.1" in low:
        return "Windows 7"
    if "windows" in low:
        return "Windows" + _bitness_suffix(hints, low)

    m = re.search(r"mac os x\s*([0-9]+)[._]([0-9]+)(?:[._]([0-9]+))?", low)
    if m:
        return f"macOS {_ver_join(m.group(1), m.group(2))}"
    if "macintosh" in low or "mac os" in low:
        if "chrome/" in low or "crios/" in low:
            return "macOS 11+"
        return "macOS"

    if "cros" in low:
        return "ChromeOS"
    if "linux" in low:
        return "Linux"

    if re.search(r"v2rayng|nekobox|hiddifynext|hiddify|okhttp", low):
        return "Android"
    if re.search(r"v2rayn(?!g)", low):
        return "Windows"
    if re.search(r"\bv2box/", low) or re.search(
        r"streisand|shadowrocket|quantumult|foxray|\bstash\b", low
    ):
        return "iOS"

    return "نامشخص"


def parse_client_app(user_agent: str, hints: dict[str, str] | None = None) -> str:
    """VPN app name, else browser name, else OS family."""
    ua = user_agent or ""
    for name, pat in _CLIENT_PATTERNS:
        if pat.search(ua):
            return name
    browser = parse_browser_name(ua, hints)
    if browser:
        return browser
    low = ua.lower()
    plat = ((hints or {}).get("platform") or "").lower()
    if plat == "android" or "android" in low:
        return "Android"
    if plat in ("ios", "iphone") or "iphone" in low:
        return "iOS"
    if plat in ("ipad", "ipados") or "ipad" in low:
        return "iPadOS"
    if plat == "windows" or "windows" in low:
        return "Windows"
    if "mac os" in plat or "macintosh" in low:
        return "macOS"
    if "linux" in low:
        return "Linux"
    return "سایر"


def detect_client_channel(user_agent: str, client_app: str = "") -> str:
    app = (client_app or "").strip()
    ua = user_agent or ""
    low = ua.lower()
    if app and app in KNOWN_APP_NAMES:
        return "app"
    if app and app not in ("سایر", "نامشخص", "Android", "iOS", "iPadOS", "Windows", "macOS", "Linux", "مرورگر"):
        if not parse_browser_name(app):
            return "app"
    if re.search(r"okhttp|dart:io|sing-box|v2ray|clash|hiddify", low):
        return "app"
    if parse_browser_name(ua) or _BROWSER_UA.search(ua):
        return "browser"
    if app:
        return "app"
    return "unknown"


def device_label(brand: str, model: str | None = None, os_name: str = "") -> str:
    """Short display line for the sub card."""
    b = (brand or "").strip() or "نامشخص"
    m = (model or "").strip()
    if m:
        return f"{b} · {m}"
    if b == "Android":
        return "Android · مدل در UA نیست"
    return b


def parse_client_device(
    user_agent: str, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    """Full device snapshot used by sub analytics."""
    hints = extract_client_hints(headers)
    ua = user_agent or ""
    brand = parse_device_brand(ua, hints)
    model = parse_device_model(ua, hints)
    os_name = parse_client_os(ua, hints)
    app = parse_client_app(ua, hints)
    browser = parse_browser_name(ua, hints)
    channel = detect_client_channel(ua, app)
    label = device_label(brand=brand, model=model, os_name=os_name)

    mobile = hints.get("mobile", "")
    low = ua.lower()
    if mobile == "?1":
        form = "phone"
    elif mobile == "?0":
        form = "desktop"
    elif "ipad" in low:
        form = "tablet"
    elif "iphone" in low or "android" in low:
        form = "phone"
    elif "windows" in low or "macintosh" in low or "x11" in low:
        form = "desktop"
    else:
        form = "unknown"

    return {
        "brand": brand,
        "model": model,
        "device_label": label,
        "os": os_name,
        "app": app,
        "browser": browser,
        "channel": channel,
        "form_factor": form,
        "hints": hints,
    }
