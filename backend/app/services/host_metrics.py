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
_prev_proc_cpu: tuple[int, int] | None = None  # idle, total jiffies
_psutil_repair_tried = False


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
    """Probe once every 60s — if dbus is wedged, skip systemctl entirely.

    Dashboard service rows for astra/nginx/xray already use /proc heuristics, so
    a slow dbus must not flip the whole host card into a scary «systemd down»
    warning. Only treat hard dbus transport failures as unusable.
    """
    global _SYSTEMCTL_OK, _SYSTEMCTL_PROBE_AT
    now = time.monotonic()
    if _SYSTEMCTL_OK is not None and (now - _SYSTEMCTL_PROBE_AT) < 60:
        return _SYSTEMCTL_OK
    _SYSTEMCTL_PROBE_AT = now
    try:
        r = subprocess.run(
            ["systemctl", "is-system-running"],
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        out = ((r.stdout or "") + (r.stderr or "")).lower()
        # Hard dbus failure only — "degraded"/"starting" are fine (nonzero rc).
        if "transport endpoint is not connected" in out:
            _SYSTEMCTL_OK = False
        else:
            _SYSTEMCTL_OK = True
    except subprocess.TimeoutExpired:
        # Slow dbus ≠ broken; keep last known or assume usable for heuristics UI.
        if _SYSTEMCTL_OK is None:
            _SYSTEMCTL_OK = True
    except Exception:  # noqa: BLE001
        if _SYSTEMCTL_OK is None:
            _SYSTEMCTL_OK = True
    return bool(_SYSTEMCTL_OK)


def _xray_listen_port(panel_id: str) -> int | None:
    """Read inbound port from panel config.json (fast, no DB)."""
    import json

    cfg = Path(f"/opt/astra/xray/panel_{panel_id}/config.json")
    if not cfg.is_file():
        return None
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    for ib in data.get("inbounds") or []:
        if not isinstance(ib, dict):
            continue
        try:
            port = int(ib.get("port") or 0)
        except (TypeError, ValueError):
            continue
        if port > 0:
            return port
    return None


def _xray_panel_running(panel_id: str, cmds: list[str]) -> bool:
    """True when xray for this panel is live (process, listen port, or unit)."""
    pid = str(panel_id).strip()
    if not pid.isdigit():
        return False
    if _cmd_has(
        cmds,
        f"/opt/astra/xray/panel_{pid}/config.json",
        f"/opt/astra/xray/panel_{pid}/",
        f"panel_{pid}/config.json",
    ):
        return True
    port = _xray_listen_port(pid)
    if port and _port_listening(int(port)):
        return True
    if _systemctl_usable():
        try:
            r = subprocess.run(
                ["systemctl", "is-active", f"astra-xray@{pid}"],
                capture_output=True,
                text=True,
                timeout=0.35,
            )
            if (r.stdout or "").strip().lower() == "active":
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


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
        # Sub path: Edge(Iran) often holds `ssh -L :18081→foreign:2086` because
        # Foreign→Iran:22 reverse SSH times out. Count reverse ssh if present,
        # else CF quick-tunnel / tunnel watchdog as the live sub path.
        if _cmd_has(
            cmds,
            "18081:127.0.0.1:2086",
            "astra_sub_rtunnel",
            "astra-sub-rtunnel-run",
        ):
            return "active"
        if _cmd_has(cmds, "cloudflared tunnel", "astra-cf-sub-run"):
            return "active"
        if _cmd_has(cmds, "astra-sub-rtunnel-watchdog", "astra-sub-tunnel-watch"):
            return "active"
        return "inactive"
    if name.startswith("astra-xray@"):
        panel_id = name.replace("astra-xray@", "").replace(".service", "")
        return "active" if _xray_panel_running(panel_id, cmds) else "inactive"

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
        for p in sorted(xdir.glob("panel_*"), key=lambda x: x.name):
            if not p.is_dir():
                continue
            pid = p.name.replace("panel_", "")
            if not pid.isdigit():
                continue
            # Skip empty dirs with no config (never started)
            if not (p / "config.json").is_file():
                continue
            unit = f"astra-xray@{pid}"
            out.append({"name": unit, "state": _service_active(unit, cmds)})
            if len(out) >= 80:
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


def _cpu_percent_from_proc() -> float:
    """Non-blocking CPU% via /proc/stat deltas (needs ≥2 samples)."""
    global _prev_proc_cpu
    try:
        with open("/proc/stat", encoding="utf-8") as f:
            parts = f.readline().split()
        nums = [int(x) for x in parts[1:8]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums)
        if _prev_proc_cpu is None:
            _prev_proc_cpu = (idle, total)
            # Approximate from load when first sample
            la1, _, _ = _load_avg()
            cores = max(1, os.cpu_count() or 1)
            return round(min(100.0, max(0.0, (la1 / cores) * 100.0)), 1)
        di = idle - _prev_proc_cpu[0]
        dt = total - _prev_proc_cpu[1]
        _prev_proc_cpu = (idle, total)
        if dt <= 0:
            return 0.0
        return round(max(0.0, min(100.0, (1.0 - (di / dt)) * 100.0)), 1)
    except (OSError, ValueError, IndexError):
        return 0.0


def _net_from_proc() -> dict[str, int]:
    sent = recv = ps = pr = 0
    try:
        with open("/proc/net/dev", encoding="utf-8") as f:
            for line in f:
                if ":" not in line:
                    continue
                name, rest = line.split(":", 1)
                iface = name.strip()
                if iface in ("lo",) or iface.startswith(("veth", "br-", "docker")):
                    continue
                cols = rest.split()
                if len(cols) < 16:
                    continue
                recv += int(cols[0])
                pr += int(cols[1])
                sent += int(cols[8])
                ps += int(cols[9])
    except (OSError, ValueError):
        pass
    return {
        "bytes_sent": sent,
        "bytes_recv": recv,
        "packets_sent": ps,
        "packets_recv": pr,
    }


def _process_count_proc() -> int:
    try:
        return sum(1 for p in Path("/proc").iterdir() if p.name.isdigit())
    except OSError:
        return 0


def _try_install_psutil() -> bool:
    """One-shot self-heal: install psutil into the running venv if missing."""
    global _psutil_repair_tried
    if _psutil_repair_tried:
        return False
    _psutil_repair_tried = True
    try:
        import sys

        logger.warning("psutil missing — attempting pip install into %s", sys.executable)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "psutil==6.1.1"],
            check=True,
            timeout=180,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("psutil auto-install failed: %s", exc)
        return False


def _collect_fallback() -> dict[str, Any]:
    """Full-enough metrics from Linux /proc when psutil is unavailable."""
    mem_total = mem_avail = mem_free = 0
    swap_total = swap_free = 0
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
            swap_total = kv.get("SwapTotal", 0)
            swap_free = kv.get("SwapFree", 0)
    except OSError:
        pass
    mem_used = max(0, mem_total - mem_avail)
    mem_pct = round((mem_used / mem_total) * 100, 1) if mem_total else 0.0
    swap_used = max(0, swap_total - swap_free)
    swap_pct = round((swap_used / swap_total) * 100, 1) if swap_total else 0.0

    cpu_count = os.cpu_count() or 0
    la1, la5, la15 = _load_avg()
    uptime = _read_uptime_seconds()
    cpu_pct = _cpu_percent_from_proc()

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

    disks = [
        {
            "mount": "/",
            **_bytes_dict(disk_total, disk_used, disk_free, disk_pct),
        }
    ]
    try:
        st2 = os.statvfs("/opt/astra")
        t2 = st2.f_frsize * st2.f_blocks
        if t2 and t2 != disk_total:
            f2 = st2.f_frsize * st2.f_bavail
            u2 = t2 - st2.f_frsize * st2.f_bfree
            disks.append(
                {
                    "mount": "/opt/astra",
                    **_bytes_dict(t2, u2, f2, round((u2 / t2) * 100, 1) if t2 else 0.0),
                }
            )
    except OSError:
        pass

    return {
        "cpu_percent": cpu_pct,
        "cpu_count_logical": cpu_count,
        "cpu_count_physical": None,
        "cpu_per_core": [],
        "cpu_freq_mhz": None,
        "load_avg": [round(la1, 2), round(la5, 2), round(la15, 2)],
        "memory": {
            **_bytes_dict(mem_total, mem_used, mem_free, mem_pct),
            "available": int(mem_avail),
        },
        "swap": _bytes_dict(swap_total, swap_used, swap_free, swap_pct),
        "disks": disks,
        "uptime_seconds": int(uptime),
        "boot_time": int(time.time() - uptime) if uptime else 0,
        "process_count": _process_count_proc(),
        "net": _net_from_proc(),
        "note": "",
    }


def collect_host_metrics(*, force: bool = False) -> dict[str, Any]:
    global _CACHE, _CACHE_AT
    now = time.monotonic()
    if not force and _CACHE is not None and (now - _CACHE_AT) < _CACHE_TTL:
        return _CACHE

    t0 = time.monotonic()
    engine = "fallback"
    try:
        import psutil  # noqa: F401

        core = _collect_with_psutil()
        engine = "psutil"
    except Exception as exc:  # noqa: BLE001
        logger.debug("psutil host metrics failed: %s", exc)
        if _try_install_psutil():
            try:
                import importlib
                import psutil as _ps

                importlib.reload(_ps)
                core = _collect_with_psutil()
                engine = "psutil"
            except Exception as exc2:  # noqa: BLE001
                logger.debug("psutil still unavailable after install: %s", exc2)
                core = _collect_fallback()
        else:
            core = _collect_fallback()

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
    # Service states above are process/port based for astra/nginx/xray — do not
    # surface a dbus probe timeout as a user-facing outage banner.

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
