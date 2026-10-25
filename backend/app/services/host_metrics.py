"""Live host metrics for the admin dashboard (CPU / RAM / disk / services)."""

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


def _service_active(name: str) -> str:
    """Return active / inactive / failed / unknown."""
    try:
        r = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True,
            text=True,
            timeout=2,
        )
        state = (r.stdout or "").strip().lower() or "unknown"
        if state in ("active", "inactive", "failed", "activating", "deactivating"):
            return state
        return state or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _list_xray_units() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    try:
        r = subprocess.run(
            [
                "systemctl",
                "list-units",
                "astra-xray@*",
                "--type=service",
                "--all",
                "--no-legend",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
        for line in (r.stdout or "").splitlines():
            parts = line.split()
            if not parts:
                continue
            unit = parts[0]
            if not unit.startswith("astra-xray@"):
                continue
            # columns: UNIT LOAD ACTIVE SUB DESCRIPTION...
            active = parts[2] if len(parts) > 2 else _service_active(unit)
            out.append({"name": unit, "state": active})
    except Exception:  # noqa: BLE001
        pass
    if not out:
        # Fallback: scan unit files / running by panel dirs
        xdir = Path("/opt/astra/xray")
        if xdir.is_dir():
            for p in sorted(xdir.glob("panel_*")):
                if not p.is_dir():
                    continue
                pid = p.name.replace("panel_", "")
                unit = f"astra-xray@{pid}"
                out.append({"name": unit, "state": _service_active(unit)})
    return out


def _collect_with_psutil() -> dict[str, Any]:
    import psutil

    global _cpu_primed
    if not _cpu_primed:
        psutil.cpu_percent(interval=None)
        _cpu_primed = True
        time.sleep(0.12)
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
    for extra in ("/opt/astra", "/var"):
        try:
            if Path(extra).is_dir():
                d = psutil.disk_usage(extra)
                # skip duplicate same device as root
                if extra != "/" and abs(d.total - disk.total) < 1024:
                    continue
                disks.append(
                    {
                        "mount": extra,
                        **_bytes_dict(d.total, d.used, d.free, d.percent),
                    }
                )
        except Exception:  # noqa: BLE001
            pass

    la1, la5, la15 = _load_avg()
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
        "process_count": int(len(psutil.pids())),
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

    # crude cpu from /proc/stat single snapshot → 0; leave 0 without interval
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


def collect_host_metrics() -> dict[str, Any]:
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

    services = [
        {"name": "astra", "label": "پنل Astra", "state": _service_active("astra")},
        {"name": "nginx", "label": "Nginx", "state": _service_active("nginx")},
    ]
    for u in _list_xray_units():
        panel_id = u["name"].replace("astra-xray@", "").replace(".service", "")
        services.append(
            {
                "name": u["name"],
                "label": f"Xray پنل {panel_id}",
                "state": u["state"],
            }
        )

    return {
        "ok": True,
        "engine": engine,
        "hostname": hostname,
        "os": os_name,
        "arch": arch,
        "python": platform.python_version(),
        "collected_at": int(time.time()),
        "services": services,
        **core,
    }
