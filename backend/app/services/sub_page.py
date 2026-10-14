"""Public subscription info page — clean end-user UI (no panel/coeff internals)."""

from __future__ import annotations

import base64
import html
import io
import json
import math
import re
from typing import Any

import qrcode

from app.services.jalali import format_jalali_from_unix, to_fa_digits


def _format_bytes(n: int) -> str:
    n = int(n or 0)
    if n <= 0:
        return "۰"
    units = ["بایت", "کیلوبایت", "مگابایت", "گیگابایت", "ترابایت"]
    x = float(n)
    i = 0
    while x >= 1024 and i < len(units) - 1:
        x /= 1024
        i += 1
    if x >= 100:
        num = f"{x:.0f}"
    elif x >= 10:
        num = f"{x:.1f}"
    else:
        num = f"{x:.2f}"
    return f"{to_fa_digits(num)} {units[i]}"


def _proto(link: str) -> str:
    m = re.match(r"^([a-z0-9]+)://", (link or "").lower())
    if not m:
        return "config"
    p = m.group(1)
    aliases = {"ss": "shadowsocks", "ssr": "shadowsocks", "hy2": "hysteria2", "hysteria": "hysteria"}
    return aliases.get(p, p)


def _status_meta(status: str) -> tuple[str, str]:
    s = (status or "").lower()
    if s == "active":
        return "فعال", "ok"
    if s == "on_hold":
        return "در انتظار", "warn"
    if s == "limited":
        return "اتمام حجم", "danger"
    if s == "expired":
        return "منقضی", "danger"
    if s == "disabled":
        return "غیرفعال", "muted"
    return status or "—", "muted"


def _qr_data_url(text: str) -> str:
    # Compact QR — smaller base64 payload for weak mobile networks
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=4,
        border=1,
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0f172a", back_color="#ffffff")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _donut_svg(pct: float | None, tone: str) -> str:
    size = 132
    stroke = 10
    r = (size - stroke) / 2
    c = 2 * math.pi * r
    if pct is None:
        dash = f"0 {c}"
        show = "∞"
        sub = "نامحدود"
    else:
        p = max(0.0, min(100.0, float(pct)))
        filled = c * (p / 100.0)
        dash = f"{filled:.2f} {c:.2f}"
        show = to_fa_digits(f"{p:.0f}" if p >= 10 else f"{p:.1f}")
        sub = "مصرف‌شده"
    colors = {
        "ok": "#0d9488",
        "warn": "#d97706",
        "danger": "#e11d48",
        "muted": "#64748b",
    }
    col = colors.get(tone, colors["muted"])
    return f"""<svg class="donut" viewBox="0 0 {size} {size}" width="{size}" height="{size}" aria-hidden="true">
  <circle cx="{size/2}" cy="{size/2}" r="{r}" fill="none" stroke="currentColor" stroke-width="{stroke}" class="donut-track"/>
  <circle cx="{size/2}" cy="{size/2}" r="{r}" fill="none" stroke="{col}" stroke-width="{stroke}"
    stroke-linecap="round" stroke-dasharray="{dash}" transform="rotate(-90 {size/2} {size/2})"/>
  <text x="50%" y="47%" text-anchor="middle" class="donut-num">{show}{"٪" if pct is not None else ""}</text>
  <text x="50%" y="60%" text-anchor="middle" class="donut-sub">{sub}</text>
</svg>"""


_LINK_META: dict[str, tuple[str, str]] = {
    "channel": (
        "کانال",
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 11v2a1 1 0 0 0 1 1h1.4l1.7 4.2A1.5 1.5 0 0 0 8.5 20h.7a1 1 0 0 0 1-.8L11 14h1"/><path d="M5 8.5 20.2 4.2a1 1 0 0 1 1.3.9v9.8a1 1 0 0 1-1.3.9L5 11.5"/><path d="M5 8.5v3"/></svg>',
    ),
    "support": (
        "پشتیبان",
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 13v-1a8 8 0 0 1 16 0v1"/><rect x="2" y="12" width="4" height="7" rx="2"/><rect x="18" y="12" width="4" height="7" rx="2"/><path d="M20 19a4 4 0 0 1-4 4h-2"/><circle cx="12" cy="19" r="1" fill="currentColor" stroke="none"/></svg>',
    ),
    "app": (
        "برنامه",
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="7" y="2.5" width="10" height="19" rx="2.2"/><path d="M12 8.2v6.2"/><path d="m9.6 11.8 2.4 2.4 2.4-2.4"/><path d="M11 18.8h2"/></svg>',
    ),
}


def render_subscription_page(
    *,
    panel_name: str,
    username: str,
    status: str,
    used: int,
    limit: int,
    expire: int | None,
    sub_url: str,
    configs: list[dict[str, Any]],
    panels_usage: list[dict[str, Any]] | None = None,
    device_limit: int = 0,
    public_links: dict[str, str] | None = None,
) -> str:
    del panels_usage  # end-users must not see panel/coeff breakdown
    used_i = int(used or 0)
    limit_i = int(limit or 0)
    unlimited = limit_i <= 0
    remain = None if unlimited else max(0, limit_i - used_i)
    if unlimited:
        pct = None
        bar_tone = "muted"
    else:
        pct = min(100.0, round(used_i / limit_i * 1000) / 10) if limit_i else 0.0
        bar_tone = "ok" if pct < 70 else "warn" if pct < 90 else "danger"

    status_label, status_tone = _status_meta(status)
    expire_label = format_jalali_from_unix(expire)
    used_label = _format_bytes(used_i)
    limit_label = "نامحدود" if unlimited else _format_bytes(limit_i)
    remain_label = "نامحدود" if remain is None else _format_bytes(remain)
    count_label = to_fa_digits(len(configs))
    device_limit_i = int(device_limit or 0)

    brand = html.escape(panel_name or "Astra")
    user_esc = html.escape(username)
    sub_esc = html.escape(sub_url)
    links_json = json.dumps([c["link"] for c in configs], ensure_ascii=False)
    qr_url = _qr_data_url(sub_url)
    donut = _donut_svg(pct, bar_tone)

    device_chip = ""
    if device_limit_i > 0:
        device_chip = (
            f'<span class="badge soft">'
            f'محدودیت: {to_fa_digits(device_limit_i)} کاربر'
            f"</span>"
        )

    plinks = public_links or {}
    quick_cards: list[str] = []
    for key, (label, icon_svg) in _LINK_META.items():
        raw = str(plinks.get(key) or "").strip()
        if not raw:
            continue
        href = html.escape(raw, quote=True)
        label_esc = html.escape(label)
        quick_cards.append(
            f'<a class="qlink qlink-{html.escape(key)}" href="{href}" '
            f'target="_blank" rel="noopener noreferrer" title="{label_esc}">'
            f'<span class="qlink-ico">{icon_svg}</span>'
            f'<span class="qlink-label">{label_esc}</span>'
            f"</a>"
        )
    quick_html = ""
    if quick_cards:
        n = len(quick_cards)
        quick_joined = "\n    ".join(quick_cards)
        quick_html = f"""<section class="card links-card" aria-label="دسترسی سریع">
  <div class="links-head">
    <h2>دسترسی سریع</h2>
  </div>
  <div class="qlink-grid qlink-n{n}">
    {quick_joined}
  </div>
</section>"""

    cfg_rows = []
    for i, c in enumerate(configs):
        link = c["link"]
        is_notice = c.get("kind") == "notice"
        proto = html.escape("اطلاع" if is_notice else _proto(link))
        remark = html.escape(str(c.get("remark") or "").strip())
        preview = html.escape(
            remark
            if is_notice and remark
            else (link if len(link) <= 64 else link[:64] + "…")
        )
        idx = to_fa_digits(i + 1)
        title_html = (
            f'<span class="cfg-name">{remark}</span>' if remark and not is_notice else ""
        )
        notice_cls = " notice" if is_notice else ""
        cfg_rows.append(
            f"""<article class="cfg{notice_cls}" data-i="{i}">
  <div class="cfg-meta">
    <span class="proto{" notice" if is_notice else ""}">{proto}</span>
    <span class="idx">#{idx}</span>
    {title_html}
  </div>
  <code class="cfg-link{" notice-text" if is_notice else ""}" dir="{"rtl" if is_notice else "ltr"}" title="{html.escape(remark or link)}">{preview}</code>
  <button type="button" class="btn ghost sm" data-copy-one="{i}">کپی</button>
</article>"""
        )
    configs_html = (
        "\n".join(cfg_rows) if cfg_rows else '<p class="empty-cfg">هنوز کانفیگی آماده نیست.</p>'
    )

    banner = ""
    if status_tone == "danger" or (status or "").lower() == "disabled":
        s = (status or "").lower()
        if s == "limited":
            msg = "حجم شما تمام شده و سرویس موقتاً قطع است."
        elif s == "expired":
            msg = "اشتراک منقضی شده و سرویس قطع است."
        else:
            msg = "این اشتراک غیرفعال است."
        tone = "danger" if status_tone == "danger" else "muted"
        banner = f'<div class="banner {tone}" role="alert">{html.escape(msg)}</div>'

    return f"""<!DOCTYPE html>
<html lang="fa" dir="rtl" data-theme="dark">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<meta name="color-scheme" content="dark light"/>
<meta name="theme-color" content="#0b1220"/>
<title>{brand} · {user_esc}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Fredoka:wght@600;700&family=Vazirmatn:wght@400;500;600;700;800&display=swap" rel="stylesheet"/>
<style>
:root,[data-theme="dark"]{{
  --bg:#0b1220;--card:#121a2b;--line:#243149;--text:#e8eef7;--muted:#8b9bb4;
  --accent:#14b8a6;--danger:#f43f5e;--warn:#f59e0b;--ok:#10b981;--input:#0a101c;
  --shadow:0 1px 0 rgba(255,255,255,.04) inset,0 8px 24px rgba(0,0,0,.28);
  --font:"Vazirmatn",Tahoma,"Segoe UI",system-ui,sans-serif;
  --brand:"Fredoka","Vazirmatn",cursive,sans-serif;
  color-scheme:dark;
}}
[data-theme="light"]{{
  --bg:#f3f5f8;--card:#fff;--line:#e2e8f0;--text:#0f172a;--muted:#64748b;
  --accent:#0f766e;--danger:#e11d48;--warn:#d97706;--ok:#059669;--input:#f8fafc;
  --shadow:0 1px 2px rgba(15,23,42,.06),0 8px 20px rgba(15,23,42,.04);
  color-scheme:light;
}}
*{{box-sizing:border-box}}
html{{-webkit-text-size-adjust:100%}}
body{{
  margin:0;color:var(--text);background:var(--bg);
  font:400 15px/1.65 var(--font);
}}
.wrap{{
  max-width:640px;margin:0 auto;
  padding:max(16px,env(safe-area-inset-top)) max(14px,env(safe-area-inset-right))
    max(56px,env(safe-area-inset-bottom)) max(14px,env(safe-area-inset-left));
}}
.hero{{
  background:var(--card);border:1px solid var(--line);border-radius:16px;
  padding:18px 16px;margin-bottom:12px;box-shadow:var(--shadow);
  border-top:3px solid var(--accent);
}}
.hero-bar{{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}}
.hero-id{{min-width:0;flex:1}}
.brand{{
  margin:0;font-size:clamp(1.35rem,5.5vw,1.75rem);font-weight:700;
  letter-spacing:-.02em;color:var(--text);font-family:var(--brand);
}}
.user-pill{{
  display:inline-block;margin-top:8px;padding:5px 10px;border-radius:8px;
  background:var(--input);border:1px solid var(--line);font-size:.82rem;color:var(--muted);
  max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}}
.user-pill b{{color:var(--text);font-weight:700}}
.theme-btn{{
  flex-shrink:0;width:40px;height:40px;border-radius:10px;border:1px solid var(--line);
  background:var(--input);color:var(--muted);cursor:pointer;display:grid;place-items:center;padding:0;
}}
.theme-btn svg{{width:18px;height:18px;display:block}}
.theme-btn .ico-sun{{display:none}}
[data-theme="light"] .theme-btn .ico-moon{{display:none}}
[data-theme="light"] .theme-btn .ico-sun{{display:block}}
.chips{{display:flex;flex-wrap:wrap;gap:6px;margin-top:14px}}
.badge{{
  display:inline-flex;align-items:center;gap:5px;padding:5px 10px;border-radius:999px;
  border:1px solid var(--line);font-size:.75rem;font-weight:600;background:var(--input);white-space:nowrap;
}}
.badge.ok{{color:var(--ok);border-color:var(--ok);background:transparent}}
.badge.warn{{color:var(--warn);border-color:var(--warn);background:transparent}}
.badge.danger{{color:var(--danger);border-color:var(--danger);background:transparent}}
.badge.muted,.badge.soft{{color:var(--muted)}}
.banner{{
  padding:12px 14px;border-radius:12px;margin-bottom:12px;font-size:.88rem;line-height:1.6;
  border:1px solid var(--danger);background:var(--card);color:var(--danger);
}}
.banner.muted{{border-color:var(--line);color:var(--muted)}}
.usage-card,.card{{
  background:var(--card);border:1px solid var(--line);border-radius:16px;
  padding:16px;margin-bottom:12px;box-shadow:var(--shadow);
}}
.usage-card{{display:grid;grid-template-columns:auto 1fr;gap:16px;align-items:center}}
.donut{{color:var(--line);display:block;width:min(132px,36vw);height:auto}}
.donut-num{{fill:var(--text);font-weight:700;font-size:22px}}
.donut-sub{{fill:var(--muted);font-size:10px}}
.usage-side{{min-width:0}}
.remain-label{{margin:0 0 2px;color:var(--muted);font-size:.78rem}}
.remain-value{{margin:0 0 12px;font-size:clamp(1.35rem,4.8vw,1.85rem);font-weight:700;letter-spacing:-.02em;line-height:1.2}}
.stats-mini{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}
.stat-mini{{padding:10px;border-radius:10px;border:1px solid var(--line);background:var(--input);min-width:0}}
.stat-mini span{{display:block;color:var(--muted);font-size:.7rem}}
.stat-mini b{{display:block;margin-top:3px;font-size:.86rem;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.card h2{{margin:0 0 4px;font-size:1rem;font-weight:700}}
.card .hint{{margin:0 0 12px;color:var(--muted);font-size:.8rem;line-height:1.5}}
.links-card{{padding:12px 14px}}
.links-head h2{{margin:0 0 10px;font-size:.8rem;color:var(--muted);font-weight:600}}
.qlink-grid{{display:grid;gap:8px;grid-template-columns:repeat(3,minmax(0,1fr))}}
.qlink-grid.qlink-n1{{grid-template-columns:1fr}}
.qlink-grid.qlink-n2{{grid-template-columns:repeat(2,minmax(0,1fr))}}
.qlink{{
  display:flex;flex-direction:column;align-items:center;gap:6px;padding:12px 8px;
  border-radius:12px;border:1px solid var(--line);background:var(--input);
  text-decoration:none;color:inherit;text-align:center;-webkit-tap-highlight-color:transparent;
}}
.qlink-ico{{
  width:34px;height:34px;border-radius:10px;display:grid;place-items:center;
  color:var(--accent);background:var(--card);border:1px solid var(--line);
}}
.qlink-channel .qlink-ico{{color:#0ea5e9}}
.qlink-support .qlink-ico{{color:#8b5cf6}}
.qlink-app .qlink-ico{{color:#10b981}}
.qlink-ico svg{{width:16px;height:16px;display:block}}
.qlink-label{{font-weight:700;font-size:.75rem;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.sub-grid{{display:grid;grid-template-columns:auto 1fr;gap:14px;align-items:center}}
.qr-frame{{padding:8px;border-radius:12px;background:#fff;border:1px solid var(--line);line-height:0}}
.qr-frame img{{display:block;width:120px;height:120px;border-radius:4px}}
.sub-side{{min-width:0;width:100%}}
input#sub{{
  width:100%;padding:10px 12px;border-radius:10px;border:1px solid var(--line);
  background:var(--input);color:var(--text);font:12px/1.4 ui-monospace,Menlo,Consolas,monospace;
  direction:ltr;text-align:left;margin-bottom:8px;
}}
.sub-actions{{display:flex;gap:8px}}
.btn{{
  appearance:none;border:0;cursor:pointer;border-radius:10px;padding:10px 14px;
  font:inherit;font-weight:700;display:inline-flex;align-items:center;justify-content:center;
  background:var(--accent);color:#fff;text-decoration:none;
}}
.btn:disabled{{opacity:.45;cursor:not-allowed}}
.btn.ghost{{background:transparent;color:var(--text);border:1px solid var(--line)}}
.btn.sm{{padding:7px 10px;font-size:.78rem;font-weight:600}}
.cfg-head{{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:10px}}
.cfg-head h2{{margin:0}}
.cfg-list{{display:flex;flex-direction:column;gap:6px}}
.cfg{{
  display:grid;grid-template-columns:1fr auto;gap:6px 8px;align-items:center;
  padding:10px;border-radius:10px;border:1px solid var(--line);background:var(--input);
}}
.cfg.notice{{border-color:var(--warn)}}
.cfg-meta{{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:6px;align-items:center}}
.proto{{
  font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.03em;
  padding:2px 7px;border-radius:999px;border:1px solid var(--line);color:var(--accent);
}}
.proto.notice{{color:var(--warn);text-transform:none}}
.cfg-name{{font-size:.8rem;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:min(100%,200px)}}
.idx{{margin-inline-start:auto;font-size:.7rem;color:var(--muted)}}
.cfg-link{{font-size:.72rem;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}}
.cfg-link.notice-text{{color:var(--text);font-weight:600;font-size:.84rem;white-space:normal;line-height:1.4}}
.cfg.more-hidden{{display:none}}
.empty-cfg{{color:var(--muted);text-align:center;padding:16px 8px;margin:0}}
#showMore{{width:100%;margin-top:8px}}
.toast{{
  position:fixed;bottom:max(14px,env(safe-area-inset-bottom));left:50%;
  transform:translateX(-50%) translateY(140%);opacity:0;
  background:var(--card);border:1px solid var(--line);color:var(--text);
  padding:9px 14px;border-radius:10px;box-shadow:var(--shadow);font-size:.86rem;font-weight:600;
  z-index:50;pointer-events:none;white-space:nowrap;
}}
.toast.show{{transform:translateX(-50%) translateY(0);opacity:1}}
.footer{{text-align:center;color:var(--muted);font-size:.72rem;margin-top:8px}}
@media (max-width:560px){{
  .usage-card{{grid-template-columns:1fr;justify-items:center;text-align:center}}
  .stats-mini{{text-align:right}}
  .sub-grid{{grid-template-columns:1fr;justify-items:center;text-align:center}}
  .sub-actions,.cfg-head .btn{{width:100%}}
  .sub-actions .btn{{width:100%}}
}}
@media (max-width:360px){{
  .qlink-grid,.qlink-grid.qlink-n2{{grid-template-columns:1fr}}
  .qlink{{flex-direction:row;justify-content:flex-start;text-align:right;padding:10px 12px;gap:10px}}
}}
</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <div class="hero-bar">
      <div class="hero-id">
        <h1 class="brand">{brand}</h1>
        <div class="user-pill">اشتراک · <b dir="ltr">{user_esc}</b></div>
      </div>
      <button type="button" class="theme-btn" id="themeBtn" aria-label="تغییر تم" title="تغییر تم">
        <svg class="ico-moon" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M21 14.3A8.5 8.5 0 0 1 9.7 3 7 7 0 1 0 21 14.3z"/></svg>
        <svg class="ico-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
      </button>
    </div>
    <div class="chips">
      <span class="badge {status_tone}">{html.escape(status_label)}</span>
      <span class="badge soft">{html.escape(expire_label)}</span>
      <span class="badge soft">{count_label} کانفیگ</span>
      {device_chip}
    </div>
  </header>

  {banner}

  <section class="usage-card" aria-label="مصرف">
    {donut}
    <div class="usage-side">
      <p class="remain-label">حجم باقی‌مانده</p>
      <p class="remain-value">{remain_label}</p>
      <div class="stats-mini">
        <div class="stat-mini"><span>مصرف‌شده</span><b>{used_label}</b></div>
        <div class="stat-mini"><span>سقف حجم</span><b>{limit_label}</b></div>
      </div>
    </div>
  </section>

  {quick_html}

  <section class="card">
    <h2>لینک اشتراک</h2>
    <p class="hint">در v2rayNG / Hiddify / Streisand به‌عنوان Subscription وارد کنید. QR همان لینک است.</p>
    <div class="sub-grid">
      <div class="qr-frame">
        <img src="{qr_url}" width="120" height="120" alt="QR لینک ساب" loading="lazy" decoding="async"/>
      </div>
      <div class="sub-side">
        <input id="sub" readonly value="{sub_esc}" onclick="this.select()"/>
        <div class="sub-actions">
          <button type="button" class="btn" id="copySub">کپی لینک ساب</button>
        </div>
      </div>
    </div>
  </section>

  <section class="card">
    <div class="cfg-head">
      <h2>کانفیگ‌ها ({count_label})</h2>
      <button type="button" class="btn" id="copyAll" {"disabled" if not configs else ""}>کپی همه</button>
    </div>
    <p class="hint">همه را یکجا یا تک‌تک کپی کنید.</p>
    <div class="cfg-list" id="cfgList">
      {configs_html}
    </div>
  </section>

  <p class="footer">{brand}</p>
</div>
<div class="toast" id="toast" role="status"></div>
<script>
(function(){{
  var LINKS={links_json};
  var root=document.documentElement,themeBtn=document.getElementById('themeBtn'),toastEl=document.getElementById('toast'),toastTimer;
  function applyTheme(t){{
    root.setAttribute('data-theme',t);
    try{{localStorage.setItem('astra-sub-theme',t);}}catch(e){{}}
    document.querySelector('meta[name="theme-color"]').setAttribute('content',t==='light'?'#f3f5f8':'#0b1220');
  }}
  var saved=null;try{{saved=localStorage.getItem('astra-sub-theme');}}catch(e){{}}
  applyTheme(saved||((window.matchMedia&&window.matchMedia('(prefers-color-scheme: light)').matches)?'light':'dark'));
  themeBtn.addEventListener('click',function(){{applyTheme(root.getAttribute('data-theme')==='dark'?'light':'dark');}});
  function toast(msg){{
    toastEl.textContent=msg;toastEl.classList.add('show');clearTimeout(toastTimer);
    toastTimer=setTimeout(function(){{toastEl.classList.remove('show');}},1400);
  }}
  function copyText(text){{
    if(navigator.clipboard&&navigator.clipboard.writeText){{
      navigator.clipboard.writeText(text).then(function(){{toast('کپی شد');}}).catch(fallback);
    }} else fallback();
    function fallback(){{
      var ta=document.createElement('textarea');ta.value=text;document.body.appendChild(ta);ta.select();
      try{{document.execCommand('copy');toast('کپی شد');}}catch(e){{toast('کپی ناموفق');}}
      ta.remove();
    }}
  }}
  document.getElementById('copySub').addEventListener('click',function(){{copyText(document.getElementById('sub').value);}});
  document.getElementById('copyAll').addEventListener('click',function(){{if(LINKS.length)copyText(LINKS.join('\\n'));}});
  document.getElementById('cfgList').addEventListener('click',function(e){{
    var btn=e.target.closest('[data-copy-one]');if(!btn)return;
    var i=Number(btn.getAttribute('data-copy-one'));if(LINKS[i])copyText(LINKS[i]);
  }});
  var items=document.querySelectorAll('#cfgList .cfg');
  if(items.length>8){{
    for(var i=8;i<items.length;i++) items[i].classList.add('more-hidden');
    var more=document.createElement('button');
    more.type='button';more.className='btn ghost';more.id='showMore';
    more.textContent='نمایش همه ('+items.length+')';
    document.getElementById('cfgList').appendChild(more);
    more.addEventListener('click',function(){{
      for(var j=0;j<items.length;j++) items[j].classList.remove('more-hidden');
      more.remove();
    }});
  }}
}})();
</script>
</body>
</html>"""
