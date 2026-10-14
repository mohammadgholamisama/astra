"""Issue Let's Encrypt certs via astra-helper (official Linux install only)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.services import site_settings as site_svc

logger = logging.getLogger(__name__)

_SAFE_DOMAIN = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$"
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


async def _run(cmd: list[str], *, timeout: float = 300) -> tuple[int, str, str]:
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


async def issue_ssl(db: AsyncSession, *, target: str, domain: str) -> dict:
    """Issue SSL for panel or sub domain and sync URLs in DB + helper env."""
    if not available():
        raise ValueError(
            "صدور SSL فقط روی سرور با نصب رسمی Astra فعال است (astra-helper)"
        )

    t = (target or "").strip().lower()
    if t not in ("panel", "sub"):
        raise ValueError("target باید panel یا sub باشد")

    d = (domain or "").strip().lower().rstrip(".")
    if not d or not _SAFE_DOMAIN.match(d):
        raise ValueError("دامنه نامعتبر است")

    helper = helper_path()
    # Prefer passwordless sudo as configured by install.sh
    code, out, err = await _run(["sudo", "-n", helper, "set-env", f"ASTRA_{t.upper()}_DOMAIN", d])
    if code != 0:
        code, out, err = await _run([helper, "set-env", f"ASTRA_{t.upper()}_DOMAIN", d])
    if code != 0:
        raise RuntimeError((err or out or "set-env failed").strip()[:800])

    code, out, err = await _run(["sudo", "-n", helper, "issue-ssl", d], timeout=420)
    if code != 0:
        code, out, err = await _run([helper, "issue-ssl", d], timeout=420)
    if code != 0:
        msg = (err or out or "certbot failed").strip()
        logger.error("SSL issue failed for %s: %s", d, msg)
        raise RuntimeError(msg[:1200])

    https_url = f"https://{d}"
    if t == "panel":
        await site_svc.update_settings(db, panel_base_url=https_url)
    else:
        await site_svc.update_settings(db, sub_base_url=https_url)

    return {
        "ok": True,
        "message": f"گواهی SSL برای {d} صادر شد",
        "domain": d,
        "target": t,
    }
