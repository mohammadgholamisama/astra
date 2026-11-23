"""Check / apply panel updates via astra-helper (official install only)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_REPO = "mohammadgholamisama/astra"
_VERSION_URL = f"https://raw.githubusercontent.com/{_REPO}/main/VERSION"
_SAFE_VER = re.compile(r"^[0-9A-Za-z._+-]{1,32}$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

# Check budget: dashboard must answer well under nginx/proxy timeouts.
_CHECK_HTTP_TIMEOUT = httpx.Timeout(2.5, connect=1.5)
_CHECK_OVERALL_SEC = 6.0
_SHA_WAVE_SEC = 2.5

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


def _version_from_body(body: str) -> str:
    if not body:
        return ""
    remote = body.strip().splitlines()[0].strip().strip("\ufeff")
    if remote and _SAFE_VER.match(remote):
        return remote
    return ""


def _astra_root() -> Path:
    # Official install; helper lives next to the tree we care about.
    return Path("/opt/astra")


async def _http_get_text(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> str:
    try:
        res = await client.get(url, headers=headers)
        if res.status_code != 200:
            return ""
        return res.text or ""
    except Exception:  # noqa: BLE001
        return ""


async def _http_get_version(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> str:
    return _version_from_body(await _http_get_text(client, url, headers=headers))


async def _fetch_head_sha(client: httpx.AsyncClient, bust: str) -> str:
    """Resolve main tip SHA — API and git ls-remote race; first valid wins."""

    async def from_api() -> str:
        body = await _http_get_text(
            client,
            f"https://api.github.com/repos/{_REPO}/commits/main?_={bust}",
            headers={"Accept": "application/vnd.github+json"},
        )
        if not body:
            return ""
        try:
            data = json.loads(body)
            sha = str((data or {}).get("sha") or "").strip()
            return sha if _SHA_RE.fullmatch(sha) else ""
        except json.JSONDecodeError:
            return ""

    async def from_ls_remote() -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "ls-remote",
                f"https://github.com/{_REPO}.git",
                "refs/heads/main",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=2.5)
            if proc.returncode == 0 and out_b:
                sha = out_b.decode().split()[0].strip()
                return sha if _SHA_RE.fullmatch(sha) else ""
        except Exception:  # noqa: BLE001
            return ""
        return ""

    tasks = [
        asyncio.create_task(from_api()),
        asyncio.create_task(from_ls_remote()),
    ]
    try:
        while tasks:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED, timeout=_SHA_WAVE_SEC
            )
            if not done:
                break
            for t in done:
                try:
                    sha = t.result()
                except Exception:  # noqa: BLE001
                    sha = ""
                if sha:
                    for p in pending:
                        p.cancel()
                    return sha
            tasks = list(pending)
    finally:
        for t in tasks:
            t.cancel()
    return ""


async def _fetch_remote_version() -> str:
    """Fetch newest VERSION from mirrors concurrently; pick max semver within budget."""
    bust = str(int(time.time()))
    found: list[str] = []

    headers_nocache = {
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    async with httpx.AsyncClient(
        timeout=_CHECK_HTTP_TIMEOUT,
        follow_redirects=True,
        headers=headers_nocache,
        limits=httpx.Limits(max_connections=12, max_keepalive_connections=6),
    ) as client:
        main_sources: list[tuple[str, dict[str, str] | None]] = [
            (
                f"https://api.github.com/repos/{_REPO}/contents/VERSION?ref=main&_={bust}",
                {"Accept": "application/vnd.github.raw"},
            ),
            (f"{_VERSION_URL}?_={bust}", None),
            (f"https://cdn.jsdelivr.net/gh/{_REPO}@main/VERSION?_={bust}", None),
            (f"https://fastly.jsdelivr.net/gh/{_REPO}@main/VERSION?_={bust}", None),
        ]

        async def collect(sources: list[tuple[str, dict[str, str] | None]]) -> list[str]:
            results = await asyncio.gather(
                *[
                    _http_get_version(client, url, headers=hdrs)
                    for url, hdrs in sources
                ],
                return_exceptions=True,
            )
            out: list[str] = []
            for r in results:
                if isinstance(r, str) and r:
                    out.append(r)
            return out

        async def sha_wave() -> list[str]:
            sha = await _fetch_head_sha(client, bust)
            if not sha:
                return []
            return await collect(
                [
                    (
                        f"https://api.github.com/repos/{_REPO}/contents/VERSION?ref={sha}&_={bust}",
                        {"Accept": "application/vnd.github.raw"},
                    ),
                    (f"https://raw.githubusercontent.com/{_REPO}/{sha}/VERSION?_={bust}", None),
                    (f"https://cdn.jsdelivr.net/gh/{_REPO}@{sha}/VERSION", None),
                ]
            )

        main_task = asyncio.create_task(collect(main_sources))
        sha_task = asyncio.create_task(sha_wave())
        try:
            done, pending = await asyncio.wait(
                {main_task, sha_task},
                timeout=_CHECK_OVERALL_SEC,
            )
            for t in pending:
                t.cancel()
            for t in done:
                try:
                    found.extend(t.result())
                except Exception:  # noqa: BLE001
                    continue
        finally:
            for t in (main_task, sha_task):
                if not t.done():
                    t.cancel()

    best = _newest_version(*found)
    if best:
        logger.info("update check remote candidates=%s → %s", found, best)
    return best


async def _git_behind_fast() -> bool:
    """True if local HEAD ≠ origin/main tip (ls-remote only — no git fetch)."""
    root = _astra_root()
    if not (root / ".git").is_dir():
        return False

    async def local_sha() -> str:
        code, out, _ = await _run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            timeout=2.0,
        )
        sha = (out or "").strip()
        return sha if code == 0 and _SHA_RE.fullmatch(sha) else ""

    async def remote_sha() -> str:
        code, out, _ = await _run(
            [
                "git",
                "-C",
                str(root),
                "ls-remote",
                "origin",
                "refs/heads/main",
            ],
            timeout=2.5,
        )
        if code != 0 or not out.strip():
            # public URL fallback when origin remote is missing/broken
            code, out, _ = await _run(
                [
                    "git",
                    "ls-remote",
                    f"https://github.com/{_REPO}.git",
                    "refs/heads/main",
                ],
                timeout=2.5,
            )
        if code != 0 or not out.strip():
            return ""
        sha = out.split()[0].strip()
        return sha if _SHA_RE.fullmatch(sha) else ""

    local, remote = await asyncio.gather(local_sha(), remote_sha())
    return bool(local and remote and local != remote)


async def check_update() -> dict:
    """Return update status quickly via concurrent HTTP (+ optional git tip compare)."""
    from app.services.version_info import get_version

    local = get_version()
    remote = ""
    git_behind = False

    remote_task = asyncio.create_task(_fetch_remote_version())
    behind_task = asyncio.create_task(_git_behind_fast())
    try:
        try:
            remote = await asyncio.wait_for(
                asyncio.shield(remote_task), timeout=_CHECK_OVERALL_SEC + 0.5
            )
        except asyncio.TimeoutError:
            remote_task.cancel()
            remote = ""

        # Don't let git tip compare delay the response once VERSION is known.
        if behind_task.done():
            try:
                git_behind = bool(behind_task.result())
            except Exception:  # noqa: BLE001
                git_behind = False
        else:
            try:
                git_behind = bool(
                    await asyncio.wait_for(asyncio.shield(behind_task), timeout=0.4)
                )
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                behind_task.cancel()
                git_behind = False
    finally:
        for t in (remote_task, behind_task):
            if not t.done():
                t.cancel()

    check_ok = bool(remote)
    if check_ok:
        # Same VERSION must NOT show «نسخه جدید» just because git tip differs
        # (unpushed/ops commits). Only a higher remote VERSION is an update.
        ver_newer = is_newer(remote, local)
        same_ver = _parse_semver(remote) == _parse_semver(local)
        update_available = bool(ver_newer)
        if update_available:
            message = f"آخرین نسخه: {remote}"
        elif same_ver and git_behind:
            message = "پنل به‌روز است (نسخه یکسان)"
        else:
            message = "پنل به‌روز است"
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
    """Kick off update without waiting — waiting deadlocks with astra restart.

    Panel used to `systemctl start astra-update` (blocking). The oneshot runs
    apply-update → restart astra while uvicorn is still waiting on that start →
    systemd deadlock / nginx 504, so the dashboard looked like "نصب نشد".
    """
    if not available():
        raise ValueError(
            "آپدیت از پنل فقط روی سرور با نصب رسمی Astra فعال است (astra-helper)"
        )

    from app.services.version_info import clear_version_cache, get_version

    local_before = get_version()
    started = False
    last_err = ""

    # 1) oneshot --no-block (never blocking start — that deadlocks with astra restart)
    for systemctl in ("/bin/systemctl", "/usr/bin/systemctl", "systemctl"):
        code, out, err = await _run(
            ["sudo", "-n", systemctl, "start", "--no-block", "astra-update.service"],
            timeout=30,
        )
        combined = (err or "") + (out or "")
        if _sudo_blocked(combined):
            raise RuntimeError(_NNP_HINT)
        if code == 0:
            started = True
            break
        last_err = combined.strip() or f"exit {code}"
        if "not allowed" in combined.lower() or "a password is required" in combined.lower():
            # old sudoers without --no-block — fall through to detached helper
            break
        if code == 5 or "not found" in combined.lower() or "could not be found" in combined.lower():
            last_err = "astra-update.service missing"
            break

    # 2) Detached helper (works with existing sudoers: astra-helper *)
    if not started:
        helper = helper_path()
        try:
            proc = await asyncio.create_subprocess_exec(
                "sudo",
                "-n",
                helper,
                "apply-update",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            await asyncio.sleep(0.3)
            if proc.returncode is not None and proc.returncode != 0:
                raise RuntimeError(last_err or "failed to start update")
            started = True
        except FileNotFoundError as exc:
            raise RuntimeError(last_err or str(exc)) from exc

    if not started:
        raise RuntimeError(last_err or "failed to start update")

    clear_version_cache()
    return {
        "ok": True,
        "started": True,
        "message": (
            "آپدیت شروع شد. ۱ تا ۳ دقیقه صبر کنید تا بیلد تمام شود؛ "
            "سپس صفحه را رفرش کنید. دیتابیس و تنظیمات حفظ می‌شوند."
        ),
        "local_version": local_before,
        "remote_version": "",
    }
