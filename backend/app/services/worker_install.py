"""Auto-install Astra Worker on a remote VPS via SSH (password auth)."""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass

logger = logging.getLogger("astra.worker.install")


@dataclass
class InstallResult:
    ok: bool
    log: str
    base_url: str | None = None
    detected_ip: str | None = None


def _sanitize_host(host: str) -> str:
    h = (host or "").strip()
    h = h.replace("https://", "").replace("http://", "")
    h = h.split("/")[0].split("@")[-1]
    # strip accidental :port from host field (ssh_port is separate)
    if h.count(":") == 1 and not h.startswith("["):
        left, right = h.rsplit(":", 1)
        if right.isdigit():
            h = left
    return h.strip()


async def ssh_install_worker(
    *,
    host: str,
    username: str,
    password: str,
    master_url: str,
    token: str,
    listen_port: int = 2090,
    name: str = "astra-worker",
    ssh_port: int = 22,
    timeout: float = 420.0,
) -> InstallResult:
    """Run the public install script on the remote host over SSH.

    Password is used only for this session — never persisted by this function.
    """
    host = _sanitize_host(host)
    user = (username or "root").strip() or "root"
    pwd = password or ""
    if not host:
        return InstallResult(ok=False, log="آدرس/IP سرور خالی است")
    if not pwd:
        return InstallResult(ok=False, log="رمز SSH الزامی است")
    master = (master_url or "").rstrip("/")
    if not master:
        return InstallResult(ok=False, log="آدرس پنل اصلی (master) مشخص نیست")

    # Prefer IPv4; disable host key prompts for first-time VPS installs.
    remote_cmd = (
        f"curl -fsSL {shlex.quote(master + '/api/public/worker-install.sh')} "
        f"| bash -s -- --master {shlex.quote(master)} "
        f"--token {shlex.quote(token)} "
        f"--port {int(listen_port)} "
        f"--name {shlex.quote(name)}"
    )
    # sshpass + OpenSSH (already used for ops on this fleet)
    cmd = [
        "sshpass",
        "-e",
        "ssh",
        "-4",
        "-p",
        str(int(ssh_port)),
        "-o",
        "PreferredAuthentications=password",
        "-o",
        "PubkeyAuthentication=no",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=25",
        "-o",
        "ServerAliveInterval=10",
        f"{user}@{host}",
        remote_cmd,
    ]
    env = dict(**{k: v for k, v in __import__("os").environ.items()})
    env["SSHPASS"] = pwd

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
    except FileNotFoundError:
        return InstallResult(
            ok=False,
            log="sshpass روی سرور اصلی نصب نیست — در حال نصب…",
        )

    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return InstallResult(ok=False, log=f"نصب بیش از {int(timeout)}s طول کشید")

    log = (out_b or b"").decode("utf-8", errors="replace")
    # Never echo the password if somehow present
    if pwd and pwd in log:
        log = log.replace(pwd, "***")

    detected_ip = host
    base = f"http://{host}:{int(listen_port)}"
    # Prefer IP printed by installer
    for line in log.splitlines():
        if "base_url to:" in line or "set base_url to:" in line:
            part = line.split("http://", 1)[-1].strip()
            if part:
                base = "http://" + part.split()[0]
                detected_ip = base.split("://", 1)[-1].split(":")[0]
        if line.strip().startswith("[astra-worker] OK"):
            pass

    ok = proc.returncode == 0 and (
        "astra-worker] OK" in log
        or '"ok": true' in log.lower()
        or "/health" in log
        or "Active: active" in log
    )
    if proc.returncode != 0 and not ok:
        return InstallResult(
            ok=False,
            log=log[-4000:] or f"ssh exit={proc.returncode}",
            base_url=base,
            detected_ip=detected_ip,
        )
    return InstallResult(
        ok=True,
        log=log[-4000:],
        base_url=base,
        detected_ip=detected_ip,
    )


async def ensure_sshpass_installed() -> bool:
    """Best-effort: install sshpass on the master if missing."""
    which = await asyncio.create_subprocess_exec(
        "which",
        "sshpass",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await which.wait()
    if which.returncode == 0:
        return True
    proc = await asyncio.create_subprocess_exec(
        "apt-get",
        "install",
        "-y",
        "-qq",
        "sshpass",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    await proc.communicate()
    return proc.returncode == 0
