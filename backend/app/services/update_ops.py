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
            # non-numeric suffix → stop
            break
    return tuple(parts) if parts else (0,)


def is_newer(remote: str, local: str) -> bool:
    return _parse_semver(remote) > _parse_semver(local)


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


async def _helper(args: list[str], *, timeout: float) -> tuple[int, str, str]:
    helper = helper_path()
    code, out, err = await _run(["sudo", "-n", helper, *args], timeout=timeout)
    if code != 0 and "password" in (err + out).lower():
        code, out, err = await _run([helper, *args], timeout=timeout)
    return code, out, err


async def check_update() -> dict:
    """Return update status. Works with or without helper (HTTP VERSION fetch)."""
    from app.services.version_info import get_version

    local = get_version()
    remote = ""
    update_available = False
    message = ""
    git_behind = False

    if available():
        code, out, err = await _helper(["check-update"], timeout=60)
        if code == 0 and out.strip():
            try:
                # Prefer last JSON line
                line = out.strip().splitlines()[-1]
                data = json.loads(line)
                remote = str(data.get("remote_version") or "").strip()
                local = str(data.get("local_version") or local).strip() or local
                update_available = bool(data.get("update_available"))
                git_behind = bool(data.get("git_behind"))
                message = str(data.get("message") or "")
                return {
                    "available": True,
                    "update_available": update_available,
                    "local_version": local,
                    "remote_version": remote or local,
                    "git_behind": git_behind,
                    "message": message
                    or (
                        "نسخه جدید موجود است"
                        if update_available
                        else "پنل به‌روز است"
                    ),
                }
            except json.JSONDecodeError:
                logger.warning("check-update bad JSON: %s", out[:400])

    # Fallback: fetch VERSION from GitHub (local / no helper)
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl",
            "-fsSL",
            "--max-time",
            "15",
            _VERSION_URL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=20)
        if proc.returncode == 0:
            remote = (out_b or b"").decode().strip().splitlines()[0].strip()
            if remote and _SAFE_VER.match(remote):
                update_available = is_newer(remote, local)
                message = (
                    "نسخه جدید موجود است" if update_available else "پنل به‌روز است"
                )
            else:
                message = "نسخه ریموت نامعتبر بود"
        else:
            message = (err_b or b"").decode()[:200] or "دریافت نسخه ریموت ناموفق"
    except Exception as exc:  # noqa: BLE001
        message = f"بررسی آپدیت ممکن نشد: {exc}"

    return {
        "available": available(),
        "update_available": update_available,
        "local_version": local,
        "remote_version": remote or local,
        "git_behind": git_behind,
        "message": message,
    }


async def apply_update() -> dict:
    if not available():
        raise ValueError(
            "آپدیت از پنل فقط روی سرور با نصب رسمی Astra فعال است (astra-helper)"
        )
    code, out, err = await _helper(["apply-update"], timeout=900)
    if code != 0:
        msg = (err or out or "update failed").strip()
        logger.error("apply-update failed: %s", msg)
        raise RuntimeError(msg[:1500])
    # Try parse final JSON line
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
