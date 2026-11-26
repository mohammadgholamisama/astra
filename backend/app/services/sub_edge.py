"""Iran-edge reverse tunnel (astra-sub-rtunnel) on/off for public sub.

When the Iran edge host is offline, disable the tunnel and point DNS of the
sub hostname at Cloudflare → this panel. Re-enable when the edge is back.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import site_settings as site_svc

logger = logging.getLogger(__name__)

KEY_SUB_EDGE_MODE = "sub_edge_mode"
MODE_CLOUDFLARE = "cloudflare"
MODE_IRAN_TUNNEL = "iran_tunnel"

EDGE_HOST = (os.environ.get("ASTRA_SUB_EDGE_HOST") or "5.202.8.221").strip()
EDGE_SSH_PORT = int(os.environ.get("ASTRA_SUB_EDGE_SSH_PORT") or "22")
TUNNEL_UNIT = "astra-sub-rtunnel"
WATCH_TIMER = "astra-sub-rtunnel-watch.timer"

_HELPER_DEFAULT = "/usr/local/bin/astra-helper"


def _helper_path() -> str:
    from app.core.config import get_settings

    helper = (getattr(get_settings(), "ASTRA_HELPER", None) or "").strip()
    return helper or _HELPER_DEFAULT


def _run(cmd: list[str], timeout: float = 40) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        return (
            int(p.returncode),
            (p.stdout or b"").decode("utf-8", "replace"),
            (p.stderr or b"").decode("utf-8", "replace"),
        )
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def _helper_sub_edge(action: str) -> tuple[int, str, str]:
    helper = _helper_path()
    code, out, err = _run(["sudo", "-n", helper, "sub-edge", action], timeout=45)
    if code != 0:
        code2, out2, err2 = _run([helper, "sub-edge", action], timeout=45)
        if code2 == 0:
            return code2, out2, err2
    return code, out, err


def _systemctl_is(state: str, unit: str) -> str:
    code, out, _ = _run(["systemctl", f"is-{state}", unit], timeout=8)
    return (out or "").strip() if code == 0 else (out or "").strip() or "unknown"


def probe_edge(
    host: str = EDGE_HOST, port: int = EDGE_SSH_PORT, timeout: float = 4.0
) -> dict[str, Any]:
    """TCP reachability only — SSH/TLS may hang on a half-dead host."""
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {
                "reachable": True,
                "host": host,
                "port": port,
                "latency_ms": int((time.time() - t0) * 1000),
                "error": "",
            }
    except OSError as exc:
        return {
            "reachable": False,
            "host": host,
            "port": port,
            "latency_ms": int((time.time() - t0) * 1000),
            "error": str(exc)[:200],
        }


def get_mode_from_cache() -> str:
    with site_svc._lock:  # noqa: SLF001
        if site_svc._loaded:
            raw = (site_svc._cache.get(KEY_SUB_EDGE_MODE) or "").strip().lower()
            if raw in (MODE_CLOUDFLARE, MODE_IRAN_TUNNEL):
                return raw
    active = _systemctl_is("active", TUNNEL_UNIT)
    enabled = _systemctl_is("enabled", TUNNEL_UNIT)
    if active == "active" or enabled == "enabled":
        return MODE_IRAN_TUNNEL
    return MODE_CLOUDFLARE


def _panel_public_ip_hint() -> str:
    for url in ("https://api.ipify.org", "http://ipv4.icanhazip.com"):
        try:
            import urllib.request

            with urllib.request.urlopen(url, timeout=3) as r:  # noqa: S310
                ip = (r.read() or b"").decode("utf-8", "replace").strip()
                if ip and len(ip) < 64 and " " not in ip:
                    return ip
        except Exception:  # noqa: BLE001
            continue
    return "IP سرور پنل"


def cloudflare_instructions(*, enabling_tunnel: bool) -> list[str]:
    sub_host = site_svc.extract_hostname(site_svc.get_sub_base_url()) or "sub.vokqem.ir"
    panel_host = site_svc.extract_hostname(site_svc.get_panel_base_url()) or "p.vokqem.ir"
    panel_ip = _panel_public_ip_hint()
    if enabling_tunnel:
        return [
            "ترتیب بدون قطعی: اول تانل را از همین صفحه روشن کنید (پنل همیشه روی سرور خارج سرو می‌دهد)",
            f"بعد Cloudflare → DNS: A برای {sub_host} = {EDGE_HOST}",
            "Proxy status: DNS only (ابر خاکستری) — پروکسی خاموش",
            "تا TTL قدیمی، کاربرانی که هنوز IP خارج را دارند مستقیم به پنل وصل می‌مانند",
        ]
    return [
        "ترتیب بدون قطعی: اول DNS را عوض کنید، بعد تانل را خاموش کنید",
        f"Cloudflare → DNS: A برای {sub_host} = {panel_ip} (مثل {panel_host})",
        "Proxy status: Proxied (ابر نارنجی) — مثل دامنه پنل",
        "۱–۲ دقیقه صبر کنید تا ترافیک به سرور خارج برگردد، بعد «خاموش کردن تانل» را بزنید",
        "SSL/TLS: Full؛ لینک ساب کاربران همان می‌ماند — فقط مسیر DNS عوض می‌شود",
    ]


def status_snapshot() -> dict[str, Any]:
    mode = get_mode_from_cache()
    tunnel_active = _systemctl_is("active", TUNNEL_UNIT) == "active"
    tunnel_enabled = _systemctl_is("enabled", TUNNEL_UNIT) == "enabled"
    watch_enabled = _systemctl_is("enabled", WATCH_TIMER) in (
        "enabled",
        "static",
        "indirect",
    )
    edge = probe_edge()
    sub_host = site_svc.extract_hostname(site_svc.get_sub_base_url()) or ""
    return {
        "mode": mode,
        "tunnel_active": tunnel_active,
        "tunnel_enabled": tunnel_enabled,
        "watch_enabled": watch_enabled,
        "edge_host": EDGE_HOST,
        "edge": edge,
        "sub_base_url": site_svc.get_sub_base_url(),
        "sub_hostname": sub_host,
        "panel_base_url": site_svc.get_panel_base_url(),
        "panel_ip_hint": _panel_public_ip_hint(),
        "cf_instructions": cloudflare_instructions(
            enabling_tunnel=(mode == MODE_IRAN_TUNNEL)
        ),
    }


def _wait_tunnel_active(*, seconds: float = 12.0) -> bool:
    """Poll systemd until reverse tunnel is active (or timeout)."""
    deadline = time.time() + max(2.0, seconds)
    while time.time() < deadline:
        if _systemctl_is("active", TUNNEL_UNIT) == "active":
            return True
        time.sleep(0.7)
    return _systemctl_is("active", TUNNEL_UNIT) == "active"


async def set_mode(db: AsyncSession, *, enabled: bool) -> dict[str, Any]:
    """enabled=True → iran tunnel; False → cloudflare (tunnel off).

    Panel :2086 always keeps serving. Toggle only starts/stops the reverse
    SSH to the Iran edge — zero local downtime. DNS switch order is in
    cf_instructions (tunnel on first / DNS off first).
    """
    await site_svc.ensure_loaded(db)
    notes: list[str] = []

    if enabled:
        edge = probe_edge()
        code, out, err = _helper_sub_edge("on")
        if code != 0:
            notes.append((err or out or "sub-edge on failed").strip()[:300])
        elif out.strip():
            notes.append(out.strip()[:300])
        active = _wait_tunnel_active(seconds=14.0)
        mode = MODE_IRAN_TUNNEL
        await site_svc._upsert_key(db, KEY_SUB_EDGE_MODE, mode)  # noqa: SLF001
        await db.commit()
        await site_svc.refresh_cache(db)
        snap = status_snapshot()
        snap["ok"] = bool(snap.get("tunnel_active") or active)
        snap["message"] = (
            "تانل ایران روشن شد — حالا DNS ساب را به IP ایران بدهید (DNS only)"
            if snap.get("ok")
            else "تانل روشن نشد — سرور ایران / SSH را چک کنید"
        )
        if not edge.get("reachable"):
            notes.insert(
                0,
                f"edge {EDGE_HOST}:{EDGE_SSH_PORT} در دسترس نیست: "
                f"{edge.get('error') or 'unreachable'}",
            )
        snap["notes"] = notes
        snap["cf_instructions"] = cloudflare_instructions(enabling_tunnel=True)
        return snap

    code, out, err = _helper_sub_edge("off")
    if code != 0:
        notes.append((err or out or "sub-edge off failed").strip()[:300])
    elif out.strip():
        notes.append(out.strip()[:300])
    mode = MODE_CLOUDFLARE
    await site_svc._upsert_key(db, KEY_SUB_EDGE_MODE, mode)  # noqa: SLF001
    await db.commit()
    await site_svc.refresh_cache(db)
    snap = status_snapshot()
    snap["ok"] = not bool(snap.get("tunnel_active"))
    snap["message"] = (
        "تانل ایران خاموش شد — اگر هنوز DNS روی ایران است، فوراً به IP خارج ببرید"
    )
    snap["notes"] = notes
    snap["cf_instructions"] = cloudflare_instructions(enabling_tunnel=False)
    return snap
