"""Check / apply panel updates via astra-helper (official install only)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_REPO = "mohammadgholamisama/astra"
_VERSION_URL = f"https://raw.githubusercontent.com/{_REPO}/main/VERSION"
_SAFE_VER = re.compile(r"^[0-9A-Za-z._+-]{1,32}$")

_NNP_HINT = (
    "سرویس astra با NoNewPrivileges اجرا می‌شود و sudo کار نمی‌کند. "
    "یک‌بار با root این را بزنید:\n"
    "cd /opt/astra && sudo git pull && "
    "sudo install -m 644 deploy/astra.service /etc/systemd/system/astra.service && "
    "sudo install -m 644 deploy/astra-update.service /etc/systemd/system/astra-update.service && "
    "sudo install -m 440 deploy/sudoers.astra /etc/sudoers.d/astra && "
    "sudo install -m 755 deploy/astra-helper /usr/local/bin/astra-helper && "
    "sudo systemctl daemon-reload && sudo systemctl restart astra && "
    "sudo astra-helper apply-update"
)


def helper_path() -> str:
    s = get_settings()
    p = (getattr(s, "ASTRA_HELPER", None) or "").strip()
    if not p:
        p = "/usr/local/bin/astra-helper"
    return p


def available() -> bool:
    p = helper_path()
    return bool(p) and Path(p).is_file() and os.access(p, os.X_OK)


def _parse_semver(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in re.split(r"[.+_-]", (v or "").strip()):
        if chunk.isdigit():
            parts.append(int(chunk))
        elif not chunk:
            continue
        else:
            break
    return tuple(parts) if parts else (0,)


def is_newer(remote: str, local: str) -> bool:
    return _parse_semver(remote) > _parse_semver(local)


def _sudo_blocked(text: str) -> bool:
    t = (text or "").lower()
    return "no new privileges" in t or "nnp" in t


async def _run(cmd: list[str], *, timeout: float = 120) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return 124, "", "timeout"
    return (
        proc.returncode or 0,
        (out_b or b"").decode("utf-8", errors="replace"),
        (err_b or b"").decode("utf-8", errors="replace"),
    )


async def _helper(args: list[str], *, timeout: float, as_root: bool = True) -> tuple[int, str, str]:
    helper = helper_path()
    if not as_root:
        return await _run([helper, *args], timeout=timeout)

    code, out, err = await _run(["sudo", "-n", helper, *args], timeout=timeout)
    combined = err + out
    if _sudo_blocked(combined):
        return code, out, f"{err}\n{_NNP_HINT}"
    if code != 0 and "password" in combined.lower():
        code, out, err = await _run([helper, *args], timeout=timeout)
    return code, out, err


def _newest_version(*versions: str) -> str:
    """Return the highest semver among non-empty candidates (never a stale middle release)."""
    best = ""
    for v in versions:
        v = (v or "").strip()
        if not v or not _SAFE_VER.match(v):
            continue
        if not best or is_newer(v, best):
            best = v
    return best


async def _curl_text(url: str, *, extra_headers: list[str] | None = None) -> str:
    cmd = [
        "curl",
        "-fsSL",
        "--max-time",
        "15",
        "-H",
        "Cache-Control: no-cache",
        "-H",
        "Pragma: no-cache",
    ]
    for h in extra_headers or []:
        cmd.extend(["-H", h])
    cmd.append(url)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, _err = await asyncio.wait_for(proc.communicate(), timeout=20)
        if proc.returncode != 0:
            return ""
        return (out_b or b"").decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _version_from_body(body: str) -> str:
    if not body:
        return ""
    remote = body.strip().splitlines()[0].strip().strip("\ufeff")
    if remote and _SAFE_VER.match(remote):
        return remote
    return ""


async def _curl_version(url: str, *, extra_headers: list[str] | None = None) -> str:
    return _version_from_body(await _curl_text(url, extra_headers=extra_headers))


async def _fetch_head_sha() -> str:
    """Resolve current main tip SHA (bypasses CDN @main cache)."""
    bust = str(int(__import__("time").time()))
    # commits API
    body = await _curl_text(
        f"https://api.github.com/repos/{_REPO}/commits/main?_={bust}",
        extra_headers=["Accept: application/vnd.github+json"],
    )
    if body:
        try:
            data = json.loads(body)
            sha = str((data or {}).get("sha") or "").strip()
            if re.fullmatch(r"[0-9a-f]{7,40}", sha):
                return sha
        except json.JSONDecodeError:
            pass
    # git ls-remote fallback
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "ls-remote",
            f"https://github.com/{_REPO}.git",
            "refs/heads/main",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, _err = await asyncio.wait_for(proc.communicate(), timeout=20)
        if proc.returncode == 0 and out_b:
            sha = out_b.decode().split()[0].strip()
            if re.fullmatch(r"[0-9a-f]{7,40}", sha):
                return sha
    except Exception:  # noqa: BLE001
        pass
    return ""


async def _fetch_remote_version() -> str:
    """Fetch the newest VERSION from every reachable source; pick the max semver.

    CDN/@main often lags several releases behind. We resolve HEAD SHA first and
    read VERSION at that exact commit, then still compare against other mirrors
    and keep the highest number.
    """
    bust = str(int(__import__("time").time()))
    found: list[str] = []

    sha = await _fetch_head_sha()
    if sha:
        for url, headers in (
            (
                f"https://api.github.com/repos/{_REPO}/contents/VERSION?ref={sha}&_={bust}",
                ["Accept: application/vnd.github.raw"],
            ),
            (f"https://raw.githubusercontent.com/{_REPO}/{sha}/VERSION?_={bust}", None),
            (f"https://cdn.jsdelivr.net/gh/{_REPO}@{sha}/VERSION", None),
        ):
            v = await _curl_version(url, extra_headers=headers)
            if v:
                found.append(v)

    for url, headers in (
        (
            f"https://api.github.com/repos/{_REPO}/contents/VERSION?ref=main&_={bust}",
            ["Accept: application/vnd.github.raw"],
        ),
        (f"{_VERSION_URL}?_={bust}", None),
        (f"https://cdn.jsdelivr.net/gh/{_REPO}@main/VERSION?_={bust}", None),
        (f"https://fastly.jsdelivr.net/gh/{_REPO}@main/VERSION?_={bust}", None),
    ):
        v = await _curl_version(url, extra_headers=headers)
        if v:
            found.append(v)

    best = _newest_version(*found)
    if best:
        logger.info("update check remote candidates=%s → %s (sha=%s)", found, best, sha or "-")
    return best


async def check_update() -> dict:
    """Return update status. Always surfaces the newest remote VERSION available."""
    from app.services.version_info import get_version

    local = get_version()
    remote = ""
    update_available = False
    check_ok = False
    message = ""
    git_behind = False
    candidates: list[str] = []

    if available():
        code, out, err = await _helper(["check-update"], timeout=60, as_root=False)
        if code != 0:
            code, out, err = await _helper(["check-update"], timeout=60, as_root=True)
        if code == 0 and out.strip():
            try:
                line = out.strip().splitlines()[-1]
                data = json.loads(line)
                helper_remote = str(data.get("remote_version") or "").strip()
                local = str(data.get("local_version") or local).strip() or local
                git_behind = bool(data.get("git_behind"))
                if helper_remote:
                    candidates.append(helper_remote)
            except json.JSONDecodeError:
                logger.warning("check-update bad JSON: %s", out[:400])

    # Always gather fresh HTTP candidates and keep the highest version
    http_remote = await _fetch_remote_version()
    if http_remote:
        candidates.append(http_remote)

    remote = _newest_version(*candidates)
    check_ok = bool(remote)

    if check_ok:
        update_available = bool(git_behind) or is_newer(remote, local)
        message = (
            f"آخرین نسخه: {remote}"
            if update_available
            else "پنل به‌روز است"
        )
    else:
        update_available = False
        message = "دریافت نسخه از گیت‌هاب ناموفق — دوباره تلاش کنید"

    return {
        "available": available(),
        "check_ok": check_ok,
        "update_available": update_available,
        "local_version": local,
        "remote_version": remote,
        "git_behind": git_behind,
        "message": message,
    }


async def apply_update() -> dict:
    if not available():
        raise ValueError(
            "آپدیت از پنل فقط روی سرور با نصب رسمی Astra فعال است (astra-helper)"
        )

    # Prefer dedicated oneshot unit (waits until finished; runs as root)
    for systemctl in ("/bin/systemctl", "/usr/bin/systemctl", "systemctl"):
        code, out, err = await _run(
            ["sudo", "-n", systemctl, "start", "astra-update.service"],
            timeout=900,
        )
        combined = err + out
        if _sudo_blocked(combined):
            raise RuntimeError(_NNP_HINT)
        if code == 0:
            from app.services.version_info import clear_version_cache, get_version

            clear_version_cache()
            return {
                "ok": True,
                "message": "آپدیت نصب شد. دیتابیس و تنظیمات حفظ شدند.",
                "local_version": get_version(),
                "remote_version": get_version(),
            }
        if code == 5 or "not found" in combined.lower() or "could not be found" in combined.lower():
            # unit missing → fall through to direct helper
            break
        if code != 0:
            # try next systemctl path / fallback
            continue

    code, out, err = await _helper(["apply-update"], timeout=900, as_root=True)
    if code != 0:
        msg = (err or out or "update failed").strip()
        logger.error("apply-update failed: %s", msg)
        if _sudo_blocked(msg):
            raise RuntimeError(_NNP_HINT)
        raise RuntimeError(msg[:1500])

    remote = ""
    local = ""
    for line in reversed(out.strip().splitlines() or [""]):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                remote = str(data.get("remote_version") or "")
                local = str(data.get("local_version") or "")
                break
            except json.JSONDecodeError:
                continue
    from app.services.version_info import clear_version_cache, get_version

    clear_version_cache()
    return {
        "ok": True,
        "message": "آپدیت نصب شد. دیتابیس و تنظیمات حفظ شدند.",
        "local_version": local or get_version(),
        "remote_version": remote or get_version(),
    }
