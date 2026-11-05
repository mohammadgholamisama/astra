"""Live host metrics for the admin dashboard (CPU / RAM / disk / services).

Designed to stay fast even when systemd/DBus is wedged: never block on
``systemctl`` longer than a fraction of a second, prefer /proc + process
probes, and cache briefly so the  dashboard poll cannot stampede the host.
"""

from __future__ import annotations

import logging
import os
import platform
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("astra.host")

_cpu_primed = False
_CACHE: dict[str, Any] | None = None
_CACHE_AT = 0.0
_CACHE_TTL = 2.0
_SYSTEMCTL_OK: bool | None = None
_SYSTEMCTL_PROBE_AT = 0.0


def _bytes_dict(total: int, used: int, free: int, percent: float) -> dict[str, Any]:
    return {
        "total": int(total),
        "used": int(used),
        "free": int(free),
        "percent": round(float(percent), 1),
    }


def _read_uptime_seconds() -> float:
    try:
        with open("/proc/uptime", encoding="utf-8") as f:
            return float(f.read().split()[0])
    except OSError:
        try:
            return max(0.0, time.time() - psutil_boot())
        except Exception:  # noqa: BLE001
            return 0.0


def psutil_boot() -> float:
    try:
        import psutil

        return float(psutil.boot_time())
    except Exception:  # noqa: BLE001
        return time.time()


def _load_avg() -> tuple[float, float, float]:
    try:
        a, b, c = os.getloadavg()
        return float(a), float(b), float(c)
    except (AttributeError, OSError):
        return 0.0, 0.0, 0.0


def _snapshot_cmdlines() -> list[str]:
    """One cheap /proc pass per metrics collect — reused by service probes."""
    out: list[str] = []
    try:
        for pid in Path("/proc").iterdir():
            if not pid.name.isdigit():
                continue
            try:
                raw = (pid / "cmdline").read_bytes()
            except OSError:
                continue
            if not raw:
                continue
            out.append(raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore"))
    except OSError:
        pass
    return out


def _cmd_has(cmds: list[str], *patterns: str) -> bool:
    for cmd in cmds:
        for pat in patterns:
            if pat in cmd:
                return True
    return False


def _port_listening(port: int) -> bool:
    """True if anything is LISTENing on this TCP port (IPv4/IPv6)."""
    needle = f"{int(port):04X}"
    try:
        for path in ("/proc/net/tcp", "/proc/net/tcp6"):
            with open(path, encoding="utf-8") as f:
                next(f, None)
                for line in f:
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    if parts[3] != "0A":  # LISTEN
                        continue
                    if parts[1].rsplit(":", 1)[-1].upper() == needle:
                        return True
    except OSError:
        return False
    return False


def _systemctl_usable() -> bool:
    """Probe once every 30s — if dbus is wedged, skip systemctl entirely."""
    global _SYSTEMCTL_OK, _SYSTEMCTL_PROBE_AT
    now = time.monotonic()
    if _SYSTEMCTL_OK is not None and (now - _SYSTEMCTL_PROBE_AT) < 30:
        return _SYSTEMCTL_OK
    _SYSTEMCTL_PROBE_AT = now
    try:
        r = subprocess.run(
            ["systemctl", "is-system-running"],
            capture_output=True,
            text=True,
            timeout=0.35,
        )
        out = ((r.stdout or "") + (r.stderr or "")).lower()
        if "transport endpoint is not connected" in out or r.returncode == 124:
            _SYSTEMCTL_OK = False
        else:
            _SYSTEMCTL_OK = True
    except Exception:  # noqa: BLE001
        _SYSTEMCTL_OK = False
    return bool(_SYSTEMCTL_OK)


def _service_active(name: str, cmds: list[str] | None = None) -> str:
    """Return active / inactive / failed / unknown — never hang."""
    cmds = cmds if cmds is not None else _snapshot_cmdlines()
    # Fast process/port heuristics first (work even when dbus is dead)
    if name == "astra":
        return (
            "active"
            if _cmd_has(cmds, "uvicorn app.main:app", "uvicorn app.main")
            else "inactive"
        )
    if name == "nginx":
        if _cmd_has(cmds, "nginx: master", "nginx: worker") or _port_listening(80) or _port_listening(443):
            return "active"
        return "inactive"
    if name == "astra-sub-rtunnel" or name.startswith("astra-sub-rtunnel"):
        return (
            "active"
            if _cmd_has(cmds, "18081:127.0.0.1:2086", "astra_sub_rtunnel")
            else "inactive"
        )
    if name.startswith("astra-xray@"):
        panel_id = name.replace("astra-xray@", "").replace(".service", "")
        return (
            "active"
            if _cmd_has(cmds, f"/opt/astra/xray/panel_{panel_id}")
            else "inactive"
        )

    if not _systemctl_usable():
        return "unknown"
    try:
        r = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True,
            text=True,
            timeout=0.4,
        )
        state = (r.stdout or "").strip().lower() or "unknown"
        if state in ("active", "inactive", "failed", "activating", "deactivating"):
            return state
        return state or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _list_xray_units(cmds: list[str]) -> list[dict[str, str]]:
    """Discover astra-xray units without blocking on systemctl list-units."""
    out: list[dict[str, str]] = []
    xdir = Path("/opt/astra/xray")
    if xdir.is_dir():
        for p in sorted(xdir.glob("panel_*")):
            if not p.is_dir():
                continue
            pid = p.name.replace("panel_", "")
            if not pid.isdigit():
                continue
            unit = f"astra-xray@{pid}"
            out.append({"name": unit, "state": _service_active(unit, cmds)})
            if len(out) >= 12:
                break
    return out


def _collect_with_psutil() -> dict[str, Any]:
    import psutil

    global _cpu_primed
    if not _cpu_primed:
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)
        _cpu_primed = True
        # tiny settle so first dashboard paint isn't always 0%
        time.sleep(0.05)
    cpu_percent = float(psutil.cpu_percent(interval=None))
    per_cpu = [float(x) for x in psutil.cpu_percent(interval=None, percpu=True)]
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    boot = float(psutil.boot_time())
    uptime = max(0.0, time.time() - boot)
    net = psutil.net_io_counters()
    try:
        cf = psutil.cpu_freq()
        cpu_freq_mhz = round(cf.current, 0) if cf else None
    except Exception:  # noqa: BLE001
        cpu_freq_mhz = None

    disks: list[dict[str, Any]] = [
        {
            "mount": "/",
            **_bytes_dict(disk.total, disk.used, disk.free, disk.percent),
        }
    ]
    # Only check /opt/astra if it's a distinct mount — skip /var to keep this cheap
    try:
        if Path("/opt/astra").is_dir():
            d = psutil.disk_usage("/opt/astra")
            if abs(d.total - disk.total) >= 1024:
                disks.append(
                    {
                        "mount": "/opt/astra",
                        **_bytes_dict(d.total, d.used, d.free, d.percent),
                    }
                )
    except Exception:  # noqa: BLE001
        pass

    la1, la5, la15 = _load_avg()
    try:
        process_count = int(len(psutil.pids()))
    except Exception:  # noqa: BLE001
        process_count = 0
    return {
        "cpu_percent": round(cpu_percent, 1),
        "cpu_count_logical": int(psutil.cpu_count(logical=True) or 0),
        "cpu_count_physical": int(psutil.cpu_count(logical=False) or 0) or None,
        "cpu_per_core": [round(x, 1) for x in per_cpu],
        "cpu_freq_mhz": cpu_freq_mhz,
        "load_avg": [round(la1, 2), round(la5, 2), round(la15, 2)],
        "memory": {
            "total": int(vm.total),
            "used": int(vm.used),
            "free": int(vm.free),
            "available": int(vm.available),
            "percent": round(float(vm.percent), 1),
        },
        "swap": _bytes_dict(sw.total, sw.used, sw.free, sw.percent),
        "disks": disks,
        "uptime_seconds": int(uptime),
        "boot_time": int(boot),
        "process_count": process_count,
        "net": {
            "bytes_sent": int(net.bytes_sent if net else 0),
            "bytes_recv": int(net.bytes_recv if net else 0),
            "packets_sent": int(net.packets_sent if net else 0),
            "packets_recv": int(net.packets_recv if net else 0),
        },
    }


def _collect_fallback() -> dict[str, Any]:
    """Minimal metrics without psutil (Linux /proc)."""
    mem_total = mem_avail = mem_free = 0
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            kv: dict[str, int] = {}
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    kv[parts[0].rstrip(":")] = int(parts[1]) * 1024
            mem_total = kv.get("MemTotal", 0)
            mem_avail = kv.get("MemAvailable", kv.get("MemFree", 0))
            mem_free = kv.get("MemFree", 0)
    except OSError:
        pass
    mem_used = max(0, mem_total - mem_avail)
    mem_pct = round((mem_used / mem_total) * 100, 1) if mem_total else 0.0

    cpu_count = os.cpu_count() or 0
    la1, la5, la15 = _load_avg()
    uptime = _read_uptime_seconds()

    disk_total = disk_used = disk_free = 0
    disk_pct = 0.0
    try:
        st = os.statvfs("/")
        disk_total = st.f_frsize * st.f_blocks
        disk_free = st.f_frsize * st.f_bavail
        disk_used = disk_total - st.f_frsize * st.f_bfree
        disk_pct = round((disk_used / disk_total) * 100, 1) if disk_total else 0.0
    except OSError:
        pass

    return {
        "cpu_percent": 0.0,
        "cpu_count_logical": cpu_count,
        "cpu_count_physical": None,
        "cpu_per_core": [],
        "cpu_freq_mhz": None,
        "load_avg": [round(la1, 2), round(la5, 2), round(la15, 2)],
        "memory": {
            **_bytes_dict(mem_total, mem_used, mem_free, mem_pct),
            "available": int(mem_avail),
        },
        "swap": _bytes_dict(0, 0, 0, 0.0),
        "disks": [
            {
                "mount": "/",
                **_bytes_dict(disk_total, disk_used, disk_free, disk_pct),
            }
        ],
        "uptime_seconds": int(uptime),
        "boot_time": int(time.time() - uptime) if uptime else 0,
        "process_count": 0,
        "net": {
            "bytes_sent": 0,
            "bytes_recv": 0,
            "packets_sent": 0,
            "packets_recv": 0,
        },
        "note": "psutil نصب نیست — آمار محدود",
    }


def collect_host_metrics(*, force: bool = False) -> dict[str, Any]:
    global _CACHE, _CACHE_AT
    now = time.monotonic()
    if not force and _CACHE is not None and (now - _CACHE_AT) < _CACHE_TTL:
        return _CACHE

    t0 = time.monotonic()
    try:
        import psutil  # noqa: F401

        core = _collect_with_psutil()
        engine = "psutil"
    except Exception as exc:  # noqa: BLE001
        logger.debug("psutil host metrics failed: %s", exc)
        core = _collect_fallback()
        engine = "fallback"

    hostname = socket.gethostname()
    try:
        uname = platform.uname()
        os_name = f"{uname.system} {uname.release}"
        arch = uname.machine
    except Exception:  # noqa: BLE001
        os_name = platform.system()
        arch = platform.machine()

    cmds = _snapshot_cmdlines()
    services = [
        {"name": "astra", "label": "پنل Astra", "state": _service_active("astra", cmds)},
        {"name": "nginx", "label": "Nginx", "state": _service_active("nginx", cmds)},
        {
            "name": "astra-sub-rtunnel",
            "label": "تونل ساب",
            "state": _service_active("astra-sub-rtunnel", cmds),
        },
    ]
    # Cap xray rows so a huge panel farm cannot bloate the payload / CPU
    for u in _list_xray_units(cmds):
        panel_id = u["name"].replace("astra-xray@", "").replace(".service", "")
        services.append(
            {
                "name": u["name"],
                "label": f"Xray پنل {panel_id}",
                "state": u["state"],
            }
        )

    note = core.pop("note", None)
    if not _systemctl_usable():
        extra = "systemd موقتاً پاسخگو نیست — وضعیت از روی پروسه‌ها"
        note = f"{note} · {extra}" if note else extra

    payload = {
        "ok": True,
        "engine": engine,
        "hostname": hostname,
        "os": os_name,
        "arch": arch,
        "python": platform.python_version(),
        "collected_at": int(time.time()),
        "collect_ms": int((time.monotonic() - t0) * 1000),
        "services": services,
        **core,
    }
    if note:
        payload["note"] = note
    _CACHE = payload
    _CACHE_AT = time.monotonic()
    return payload
