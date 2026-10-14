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
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=7,
        border=2,
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0a3d38", back_color="#ffffff")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _donut_svg(pct: float | None, tone: str) -> str:
    size = 168
    stroke = 12
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
        "ok": "#2dd4bf",
        "warn": "#fbbf24",
        "danger": "#fb7185",
        "muted": "#38bdf8",
    }
    col = colors.get(tone, colors["muted"])
    return f"""<svg class="donut" viewBox="0 0 {size} {size}" width="{size}" height="{size}" aria-hidden="true">
  <circle cx="{size/2}" cy="{size/2}" r="{r}" fill="none" stroke="currentColor" stroke-width="{stroke}" class="donut-track"/>
  <circle cx="{size/2}" cy="{size/2}" r="{r}" fill="none" stroke="{col}" stroke-width="{stroke}"
    stroke-linecap="round" stroke-dasharray="{dash}" transform="rotate(-90 {size/2} {size/2})" class="donut-fill"/>
  <text x="50%" y="46%" text-anchor="middle" class="donut-num">{show}{"٪" if pct is not None else ""}</text>
  <text x="50%" y="58%" text-anchor="middle" class="donut-sub">{sub}</text>
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
<title>{brand} · {user_esc}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Fredoka:wght@500;600;700&family=Vazirmatn:wght@400;500;600;700;800&family=Syne:wght@700;800&display=swap" rel="stylesheet"/>
<style>
:root, [data-theme="dark"] {{
  --bg0:#030910; --bg1:#07141e; --bg2:#0a1a24;
  --card:rgba(8,24,36,.88); --card-solid:#0b1822;
  --line:rgba(45,212,191,.18); --text:#ecfaf6; --muted:#7fa39a;
  --accent:#2dd4bf; --accent2:#22d3ee; --accent3:#818cf8;
  --danger:#fb7185; --warn:#fbbf24; --ok:#34d399;
  --glow-a:rgba(45,212,191,.28); --glow-b:rgba(34,211,238,.16); --glow-c:rgba(129,140,248,.12);
  --input:rgba(0,0,0,.35); --shadow:0 30px 80px rgba(0,0,0,.5);
  color-scheme:dark;
}}
[data-theme="light"] {{
  --bg0:#eef6f4; --bg1:#e2efec; --bg2:#d5e6e1;
  --card:rgba(255,255,255,.92); --card-solid:#fff;
  --line:rgba(15,118,110,.14); --text:#0c221d; --muted:#567069;
  --accent:#0d9488; --accent2:#0891b2; --accent3:#6366f1;
  --danger:#e11d48; --warn:#d97706; --ok:#059669;
  --glow-a:rgba(13,148,136,.16); --glow-b:rgba(8,145,178,.12); --glow-c:rgba(99,102,241,.08);
  --input:rgba(255,255,255,.96); --shadow:0 22px 50px rgba(12,34,29,.1);
  color-scheme:light;
}}
* {{ box-sizing:border-box }}
html,body {{ min-height:100% }}
body {{
  margin:0; font-family:Vazirmatn,system-ui,sans-serif; color:var(--text);
  background:
    radial-gradient(900px 480px at 0% -10%, var(--glow-a), transparent 55%),
    radial-gradient(700px 420px at 100% 5%, var(--glow-b), transparent 50%),
    radial-gradient(600px 360px at 50% 100%, var(--glow-c), transparent 55%),
    linear-gradient(165deg, var(--bg0), var(--bg1) 45%, var(--bg2));
  background-attachment:fixed;
}}
.wrap {{
  max-width:760px; margin:0 auto;
  padding:max(18px, env(safe-area-inset-top)) max(14px, env(safe-area-inset-right))
    max(80px, env(safe-area-inset-bottom)) max(14px, env(safe-area-inset-left));
}}

.hero {{
  position:relative; overflow:hidden; border-radius:clamp(22px, 4.5vw, 30px);
  border:1px solid transparent;
  background:
    linear-gradient(var(--card), var(--card)) padding-box,
    linear-gradient(125deg, color-mix(in srgb, var(--accent) 70%, transparent), color-mix(in srgb, var(--accent2) 50%, transparent), color-mix(in srgb, var(--accent3) 45%, transparent)) border-box;
  backdrop-filter:blur(20px); box-shadow:var(--shadow);
  padding:clamp(18px, 4.2vw, 26px); margin-bottom:16px;
}}
.hero::before {{
  content:""; position:absolute; inset:0; pointer-events:none;
  background:
    radial-gradient(420px 200px at 100% -10%, color-mix(in srgb, var(--accent) 30%, transparent), transparent 70%),
    radial-gradient(320px 170px at -10% 110%, color-mix(in srgb, var(--accent2) 20%, transparent), transparent 65%);
}}
.hero-bar {{
  position:relative; z-index:1;
  display:flex; align-items:center; justify-content:space-between; gap:12px;
}}
.hero-id {{ min-width:0; flex:1 }}
.brand {{
  font-family:Fredoka,Syne,cursive,sans-serif; font-weight:700;
  font-size:clamp(1.95rem, 7.8vw, 3rem); letter-spacing:.02em; line-height:1.05; margin:0;
  background:linear-gradient(115deg, var(--accent) 0%, var(--accent2) 48%, var(--accent3) 100%);
  -webkit-background-clip:text; background-clip:text; color:transparent;
}}
.user-pill {{
  display:inline-flex; align-items:center; margin-top:12px;
  max-width:100%; padding:7px 14px; border-radius:999px;
  border:1px solid color-mix(in srgb, var(--accent) 28%, var(--line));
  background:color-mix(in srgb, var(--accent) 10%, transparent);
  font-size:.84rem; color:var(--muted);
}}
.user-pill b {{
  color:var(--text); font-weight:700; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
}}

.theme-switch {{
  position:relative; z-index:1; flex-shrink:0;
  width:72px; height:40px; padding:0; border:0; border-radius:999px;
  cursor:pointer; appearance:none;
  background:color-mix(in srgb, var(--input) 90%, transparent);
  border:1px solid var(--line);
  box-shadow:inset 0 1px 2px rgba(0,0,0,.12);
  transition:background .25s, border-color .25s, box-shadow .25s;
}}
.theme-switch:hover {{
  border-color:color-mix(in srgb, var(--accent) 45%, var(--line));
  box-shadow:0 0 0 3px color-mix(in srgb, var(--accent) 14%, transparent);
}}
.theme-switch:focus-visible {{
  outline:2px solid var(--accent); outline-offset:3px;
}}
.theme-knob {{
  position:absolute; top:3px; right:3px;
  width:32px; height:32px; border-radius:50%;
  background:linear-gradient(145deg, #fff, #e8f7f4);
  box-shadow:0 4px 12px rgba(0,0,0,.22);
  transition:transform .28s cubic-bezier(.34,1.4,.64,1), background .25s;
  z-index:2;
}}
[data-theme="light"] .theme-knob {{
  transform:translateX(-32px);
  background:linear-gradient(145deg, #fff8e7, #ffe08a);
}}
.theme-ico {{
  position:absolute; top:50%; transform:translateY(-50%);
  width:18px; height:18px; z-index:1; pointer-events:none;
  transition:opacity .2s, color .2s;
}}
.theme-ico svg {{ display:block; width:100%; height:100% }}
.theme-ico-moon {{ right:10px; color:#94a3b8 }}
.theme-ico-sun {{ left:10px; color:#f59e0b }}
[data-theme="dark"] .theme-ico-moon {{ color:#e2e8f0; opacity:1 }}
[data-theme="dark"] .theme-ico-sun {{ opacity:.35 }}
[data-theme="light"] .theme-ico-sun {{ color:#d97706; opacity:1 }}
[data-theme="light"] .theme-ico-moon {{ opacity:.35 }}
[data-theme="dark"] .theme-switch {{
  background:linear-gradient(90deg, #0f172a, #134e4a);
}}
[data-theme="light"] .theme-switch {{
  background:linear-gradient(90deg, #fef3c7, #ecfdf5);
}}

.chips {{
  position:relative; z-index:1;
  display:flex; gap:8px; margin-top:18px;
  overflow-x:auto; -webkit-overflow-scrolling:touch;
  scrollbar-width:none; padding-bottom:2px;
}}
.chips::-webkit-scrollbar {{ display:none }}
.badge {{
  display:inline-flex; align-items:center; gap:6px;
  flex:0 0 auto; padding:8px 13px; border-radius:999px;
  border:1px solid var(--line); font-size:.76rem; font-weight:600;
  background:color-mix(in srgb, var(--input) 55%, transparent);
  backdrop-filter:blur(8px); white-space:nowrap;
}}
.badge.ok {{
  color:var(--ok);
  border-color:color-mix(in srgb, var(--ok) 45%, var(--line));
  background:color-mix(in srgb, var(--ok) 12%, transparent);
}}
.badge.warn {{
  color:var(--warn);
  border-color:color-mix(in srgb, var(--warn) 45%, var(--line));
  background:color-mix(in srgb, var(--warn) 12%, transparent);
}}
.badge.danger {{
  color:var(--danger);
  border-color:color-mix(in srgb, var(--danger) 45%, var(--line));
  background:color-mix(in srgb, var(--danger) 12%, transparent);
}}
.badge.muted {{
  color:var(--muted);
  border-color:var(--line);
  background:color-mix(in srgb, var(--input) 70%, transparent);
}}
.badge.soft {{
  color:var(--text);
  border-color:color-mix(in srgb, var(--accent) 30%, var(--line));
  background:color-mix(in srgb, var(--accent) 8%, transparent);
}}

.banner {{
  padding:13px 15px; border-radius:16px; margin-bottom:14px; font-size:.9rem; line-height:1.65;
  border:1px solid color-mix(in srgb, var(--danger) 40%, var(--line));
  background:color-mix(in srgb, var(--danger) 12%, transparent); color:var(--danger);
}}
.banner.muted {{
  border-color:var(--line); color:var(--muted);
  background:color-mix(in srgb, var(--input) 70%, transparent);
}}

.usage-card {{
  display:grid; grid-template-columns:auto 1fr; gap:clamp(16px, 3.5vw, 24px); align-items:center;
  border-radius:clamp(20px, 4vw, 26px); border:1px solid var(--line); background:var(--card);
  backdrop-filter:blur(14px); box-shadow:var(--shadow);
  padding:clamp(18px, 4vw, 24px); margin-bottom:14px;
}}
.donut {{ color:color-mix(in srgb, var(--muted) 20%, transparent); display:block; width:min(168px, 42vw); height:auto }}
.donut-num {{
  fill:var(--text); font-family:Syne,Vazirmatn,sans-serif; font-weight:800; font-size:26px;
}}
.donut-sub {{ fill:var(--muted); font-size:11px; font-family:Vazirmatn,sans-serif }}
.usage-side {{ min-width:0; width:100% }}
.remain-label {{ color:var(--muted); font-size:.8rem; margin:0 0 4px }}
.remain-value {{
  font-family:Syne,Vazirmatn,sans-serif; font-weight:800;
  font-size:clamp(1.5rem, 5.2vw, 2.2rem); letter-spacing:-.02em; margin:0 0 16px; line-height:1.15;
  background:linear-gradient(115deg, var(--text), color-mix(in srgb, var(--accent) 70%, var(--text)));
  -webkit-background-clip:text; background-clip:text; color:transparent;
}}
.stats-mini {{ display:grid; grid-template-columns:1fr 1fr; gap:8px }}
.stat-mini {{
  padding:12px 13px; border-radius:14px; border:1px solid var(--line);
  background:color-mix(in srgb, var(--input) 70%, transparent); min-width:0;
}}
.stat-mini span {{ display:block; color:var(--muted); font-size:.72rem }}
.stat-mini b {{
  display:block; margin-top:4px; font-size:clamp(.82rem, 3.2vw, .94rem); font-weight:700;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
}}

.card {{
  border-radius:clamp(18px, 4vw, 24px); border:1px solid var(--line); background:var(--card);
  backdrop-filter:blur(12px); padding:clamp(15px, 3.6vw, 20px); margin-bottom:14px; box-shadow:var(--shadow);
}}
.card h2 {{
  margin:0 0 4px; font-size:1.05rem; font-family:Syne,Vazirmatn,sans-serif; font-weight:700;
}}
.card .hint {{ margin:0 0 14px; color:var(--muted); font-size:.82rem; line-height:1.55 }}

.links-card {{ padding:12px 14px 14px }}
.links-head {{ margin:0 0 10px }}
.links-head h2 {{
  margin:0; font-size:.88rem; font-weight:700; color:var(--muted);
  font-family:Vazirmatn,sans-serif; letter-spacing:.01em;
}}
.qlink-grid {{
  display:grid; gap:8px;
  grid-template-columns:repeat(3, minmax(0, 1fr));
}}
.qlink-grid.qlink-n1 {{ grid-template-columns:1fr }}
.qlink-grid.qlink-n2 {{ grid-template-columns:repeat(2, minmax(0, 1fr)) }}
.qlink {{
  display:flex; flex-direction:column; align-items:center; justify-content:center;
  gap:7px; min-height:0; padding:12px 8px 11px;
  border-radius:14px; border:1px solid var(--line);
  text-decoration:none; color:inherit; text-align:center;
  background:
    linear-gradient(160deg, color-mix(in srgb, var(--accent) 8%, transparent), transparent 55%),
    color-mix(in srgb, var(--input) 70%, transparent);
  transition:transform .14s ease, border-color .14s, background .14s, box-shadow .14s;
  -webkit-tap-highlight-color:transparent;
}}
.qlink:hover, .qlink:focus-visible {{
  transform:translateY(-1px);
  border-color:color-mix(in srgb, var(--accent) 50%, var(--line));
  box-shadow:0 8px 20px color-mix(in srgb, var(--accent) 12%, transparent);
  outline:none;
}}
.qlink:active {{ transform:scale(.97) }}
.qlink-ico {{
  width:36px; height:36px; border-radius:11px;
  display:grid; place-items:center; flex-shrink:0;
  color:var(--accent);
  background:color-mix(in srgb, var(--accent) 16%, transparent);
  border:1px solid color-mix(in srgb, var(--accent) 26%, transparent);
}}
.qlink-channel .qlink-ico {{
  color:#38bdf8;
  background:color-mix(in srgb, #38bdf8 16%, transparent);
  border-color:color-mix(in srgb, #38bdf8 28%, transparent);
}}
.qlink-support .qlink-ico {{
  color:#a78bfa;
  background:color-mix(in srgb, #a78bfa 16%, transparent);
  border-color:color-mix(in srgb, #a78bfa 28%, transparent);
}}
.qlink-app .qlink-ico {{
  color:#34d399;
  background:color-mix(in srgb, #34d399 16%, transparent);
  border-color:color-mix(in srgb, #34d399 28%, transparent);
}}
[data-theme="light"] .qlink-channel .qlink-ico {{ color:#0284c7 }}
[data-theme="light"] .qlink-support .qlink-ico {{ color:#7c3aed }}
[data-theme="light"] .qlink-app .qlink-ico {{ color:#059669 }}
.qlink-ico svg {{ width:18px; height:18px; display:block }}
.qlink-label {{
  font-weight:700; font-size:.78rem; line-height:1.2;
  max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
}}

.sub-grid {{
  display:grid; grid-template-columns:auto 1fr; gap:18px; align-items:center;
}}
.qr-frame {{
  padding:10px; border-radius:18px; background:#fff; border:1px solid var(--line);
  box-shadow:0 8px 28px rgba(0,0,0,.12); line-height:0;
}}
.qr-frame img {{ display:block; width:min(168px, 46vw); height:auto; border-radius:8px }}
.sub-side {{ min-width:0; width:100% }}
input#sub {{
  width:100%; padding:12px 14px; border-radius:12px; border:1px solid var(--line);
  background:var(--input); color:var(--text);
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:.74rem; direction:ltr; text-align:left; margin-bottom:10px;
}}
.sub-actions {{ display:flex; flex-wrap:wrap; gap:8px }}
.btn {{
  appearance:none; border:0; cursor:pointer; border-radius:12px; padding:11px 16px;
  font:inherit; font-weight:700; display:inline-flex; align-items:center; justify-content:center; gap:8px;
  background:linear-gradient(120deg, var(--accent), var(--accent2));
  color:#042f2e; text-decoration:none; transition:transform .15s, filter .15s;
}}
[data-theme="light"] .btn:not(.ghost) {{ color:#fff }}
.btn:hover {{ filter:brightness(1.06); transform:translateY(-1px) }}
.btn:disabled {{ opacity:.5; cursor:not-allowed; transform:none }}
.btn.ghost {{ background:transparent; color:var(--text); border:1px solid var(--line) }}
.btn.sm {{ padding:8px 12px; font-size:.8rem; font-weight:600 }}

.cfg-head {{
  display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap;
  margin-bottom:12px;
}}
.cfg-head h2 {{ margin:0 }}
.cfg-list {{ display:flex; flex-direction:column; gap:8px }}
.cfg {{
  display:grid; grid-template-columns:1fr auto; gap:8px 10px; align-items:center;
  padding:12px; border-radius:14px; border:1px solid var(--line);
  background:color-mix(in srgb, var(--input) 80%, transparent);
}}
.cfg.notice {{
  border-color:color-mix(in srgb, var(--warn) 45%, var(--line));
  background:color-mix(in srgb, var(--warn) 10%, transparent);
}}
.cfg-meta {{ grid-column:1 / -1; display:flex; flex-wrap:wrap; gap:6px; align-items:center }}
.proto {{
  font-size:.7rem; font-weight:700; text-transform:uppercase; letter-spacing:.04em;
  padding:3px 8px; border-radius:999px; border:1px solid var(--line); color:var(--accent);
}}
.proto.notice {{ color:var(--warn); border-color:color-mix(in srgb, var(--warn) 40%, var(--line)); text-transform:none }}
.cfg-name {{
  font-size:.82rem; font-weight:700; color:var(--text);
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:min(100%, 220px);
}}
.idx {{ margin-inline-start:auto; font-size:.72rem; color:var(--muted) }}
.cfg-link {{
  font-size:.74rem; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; min-width:0;
}}
.cfg-link.notice-text {{
  color:var(--text); font-weight:600; font-size:.86rem; white-space:normal; line-height:1.45;
}}
.empty-cfg {{ color:var(--muted); text-align:center; padding:18px 8px; margin:0 }}
.toast {{
  position:fixed; bottom:max(16px, env(safe-area-inset-bottom)); left:50%;
  transform:translateX(-50%) translateY(120%);
  background:var(--card-solid); border:1px solid var(--line); color:var(--text);
  padding:10px 16px; border-radius:12px; box-shadow:var(--shadow); font-size:.88rem; font-weight:600;
  z-index:50; transition:transform .25s ease; pointer-events:none; white-space:nowrap;
}}
.toast.show {{ transform:translateX(-50%) translateY(0) }}
.footer {{
  text-align:center; color:var(--muted); font-size:.76rem; margin-top:10px;
  font-family:Fredoka,Vazirmatn,sans-serif; letter-spacing:.02em;
}}

@media (max-width:560px) {{
  .usage-card {{ grid-template-columns:1fr; justify-items:center; text-align:center }}
  .usage-side {{ text-align:center }}
  .stats-mini {{ text-align:right }}
  .sub-grid {{ grid-template-columns:1fr; justify-items:center; text-align:center }}
  .sub-actions {{ width:100% }}
  .sub-actions .btn {{ width:100% }}
  .cfg-head {{ flex-direction:column; align-items:stretch }}
  .cfg-head .btn {{ width:100% }}
  .hero-bar {{ align-items:flex-start }}
  .theme-switch {{ width:64px; height:36px }}
  .theme-knob {{ width:28px; height:28px; top:3px; right:3px }}
  [data-theme="light"] .theme-knob {{ transform:translateX(-28px) }}
  .theme-ico {{ width:15px; height:15px }}
  .theme-ico-moon {{ right:8px }}
  .theme-ico-sun {{ left:8px }}
  .qlink {{ padding:10px 6px 9px; gap:6px }}
  .qlink-ico {{ width:32px; height:32px; border-radius:10px }}
  .qlink-ico svg {{ width:16px; height:16px }}
  .qlink-label {{ font-size:.72rem }}
}}
@media (max-width:360px) {{
  .qlink-grid, .qlink-grid.qlink-n2, .qlink-grid.qlink-n3 {{
    grid-template-columns:1fr;
  }}
  .qlink {{
    flex-direction:row; justify-content:flex-start; text-align:right;
    padding:10px 12px; gap:10px;
  }}
}}
@media (prefers-reduced-motion:reduce) {{
  .btn:hover, .toast, .theme-knob, .theme-switch, .theme-ico, .qlink {{ transition:none }}
  .qlink:hover, .qlink:focus-visible, .qlink:active {{ transform:none }}
}}
</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <div class="hero-bar">
      <div class="hero-id">
        <h1 class="brand">{brand}</h1>
        <div class="user-pill">
          <span>اشتراک · <b dir="ltr">{user_esc}</b></span>
        </div>
      </div>
      <button type="button" class="theme-switch" id="themeBtn" role="switch" aria-checked="false" aria-label="تغییر تم روشن و تاریک" title="تغییر تم">
        <span class="theme-knob" aria-hidden="true"></span>
        <span class="theme-ico theme-ico-sun" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
        </span>
        <span class="theme-ico theme-ico-moon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="currentColor"><path d="M21 14.3A8.5 8.5 0 0 1 9.7 3 7 7 0 1 0 21 14.3z"/></svg>
        </span>
      </button>
    </div>
    <div class="chips">
      <span class="badge {status_tone}">● {html.escape(status_label)}</span>
      <span class="badge soft">📅 {html.escape(expire_label)}</span>
      <span class="badge soft">🔗 {count_label} کانفیگ</span>
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
    <p class="hint">لینک را در اپ کپی کنید یا QR را اسکن کنید تا همه کانفیگ‌ها یکجا اضافه شوند.</p>
    <div class="sub-grid">
      <div class="qr-frame">
        <img src="{qr_url}" width="168" height="168" alt="QR لینک ساب"/>
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
    <p class="hint">می‌توانید همه را یکجا یا تک‌تک کپی کنید.</p>
    <div class="cfg-list" id="cfgList">
      {configs_html}
    </div>
  </section>

  <p class="footer">{brand}</p>
</div>
<div class="toast" id="toast" role="status"></div>
<script>
(function() {{
  const LINKS = {links_json};
  const root = document.documentElement;
  const themeBtn = document.getElementById('themeBtn');
  const toastEl = document.getElementById('toast');
  let toastTimer;

  function savedTheme() {{
    try {{ return localStorage.getItem('astra-sub-theme'); }} catch (e) {{ return null; }}
  }}
  function applyTheme(t) {{
    root.setAttribute('data-theme', t);
    const light = t === 'light';
    themeBtn.setAttribute('aria-checked', light ? 'true' : 'false');
    themeBtn.title = light ? 'حالت تاریک' : 'حالت روشن';
    try {{ localStorage.setItem('astra-sub-theme', t); }} catch (e) {{}}
  }}
  const preferLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
  applyTheme(savedTheme() || (preferLight ? 'light' : 'dark'));
  themeBtn.addEventListener('click', () => {{
    applyTheme(root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
  }});

  function toast(msg) {{
    toastEl.textContent = msg;
    toastEl.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove('show'), 1600);
  }}
  async function copyText(text) {{
    try {{
      await navigator.clipboard.writeText(text);
      toast('کپی شد');
    }} catch (e) {{
      const ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      try {{ document.execCommand('copy'); toast('کپی شد'); }} catch (e2) {{ toast('کپی ناموفق'); }}
      ta.remove();
    }}
  }}

  document.getElementById('copySub').addEventListener('click', () => {{
    copyText(document.getElementById('sub').value);
  }});
  document.getElementById('copyAll').addEventListener('click', () => {{
    if (!LINKS.length) return;
    copyText(LINKS.join('\\n'));
  }});
  document.getElementById('cfgList').addEventListener('click', (e) => {{
    const btn = e.target.closest('[data-copy-one]');
    if (!btn) return;
    const i = Number(btn.getAttribute('data-copy-one'));
    if (LINKS[i]) copyText(LINKS[i]);
  }});
}})();
</script>
</body>
</html>"""
