"""Multi-job background-ops progress for the admin dashboard.

Every long-running / fire-and-forget task can register itself so the
dashboard shows what is happening live (delete, add, retry, heal, …).
Process-local; resets on restart (expected).
"""

from __future__ import annotations

import threading
import time
import uuid
from copy import deepcopy
from typing import Any

_lock = threading.Lock()

# Keep finished jobs visible so admin can read coverage (active/pending/error)
_HOLD_FINISHED_SEC = 180.0
_MAX_JOBS = 32

_PHASE_DEFS: tuple[tuple[str, str], ...] = (
    ("delete", "حذف پنل‌های جدا‌شده"),
    ("add", "اضافه و همگام‌سازی پنل‌ها"),
    ("retry", "تلاش مجدد خطاها"),
)

_KIND_ICON = {
    "group_resync": "group",
    "category_resync": "group",
    "category_move": "group",
    "bulk_retry": "retry",
    "auto_retry": "retry",
    "heal_panel": "heal",
    "heal_coverage": "heal",
    "provision": "add",
    "sync_modify": "sync",
    "delete_remotes": "delete",
    "recount": "sync",
}

_jobs: dict[str, dict[str, Any]] = {}
# Latest job id per sales group (for crash/finish lookup)
_group_jobs: dict[int, str] = {}


def _empty_phases(
    *,
    only: set[str] | None = None,
    total: int = 0,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pid, label in _PHASE_DEFS:
        if only is not None and pid not in only:
            continue
        out.append(
            {
                "id": pid,
                "label": label,
                "status": "pending",
                "done": 0,
                "total": max(0, int(total)),
                "ok": 0,
                "failed": 0,
            }
        )
    return out


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _prune_locked(now: float | None = None) -> None:
    """Drop stale finished jobs; keep active + recent finished."""
    ts = time.time() if now is None else now
    dead: list[str] = []
    for jid, job in _jobs.items():
        if job.get("active"):
            continue
        finished = job.get("finished_at")
        if finished is None or (ts - float(finished)) > _HOLD_FINISHED_SEC:
            dead.append(jid)
    for jid in dead:
        _jobs.pop(jid, None)
    # Hard cap: drop oldest finished first
    if len(_jobs) > _MAX_JOBS:
        finished = sorted(
            (
                (float(j.get("finished_at") or 0), jid)
                for jid, j in _jobs.items()
                if not j.get("active")
            ),
            key=lambda x: x[0],
        )
        for _, jid in finished[: max(0, len(_jobs) - _MAX_JOBS)]:
            _jobs.pop(jid, None)


def _job_public(job: dict[str, Any]) -> dict[str, Any]:
    kind = job.get("kind")
    return {
        "id": job.get("id"),
        "active": bool(job.get("active")),
        "kind": kind,
        "kind_label": kind_label(str(kind) if kind else None),
        "icon": job.get("icon") or _KIND_ICON.get(str(kind or ""), "sync"),
        "title": job.get("title") or "",
        "message": job.get("message") or "",
        "group_id": job.get("group_id"),
        "group_name": job.get("group_name") or "",
        "phase": job.get("phase"),
        "phases": deepcopy(job.get("phases") or []),
        "done": int(job.get("done") or 0),
        "total": int(job.get("total") or 0),
        "ok": int(job.get("ok") or 0),
        "failed": int(job.get("failed") or 0),
        "users_total": int(job.get("users_total") or 0),
        "errors": list(job.get("errors") or [])[:8],
        "started_at": job.get("started_at"),
        "updated_at": job.get("updated_at"),
        "finished_at": job.get("finished_at"),
    }


def get_ops_status() -> dict[str, Any]:
    with _lock:
        _prune_locked()
        jobs = [_job_public(j) for j in _jobs.values()]
    # Active first, then newest updated
    jobs.sort(
        key=lambda j: (
            0 if j.get("active") else 1,
            -(float(j.get("updated_at") or j.get("started_at") or 0)),
        )
    )
    active_jobs = [j for j in jobs if j.get("active")]
    primary = active_jobs[0] if active_jobs else (jobs[0] if jobs else None)
    # Legacy single-job shape (dashboard v1) + multi-job list
    out: dict[str, Any] = {
        "active": bool(active_jobs),
        "jobs": jobs,
        "count_active": len(active_jobs),
    }
    if primary:
        out.update(
            {
                "kind": primary.get("kind"),
                "title": primary.get("title"),
                "group_id": primary.get("group_id"),
                "group_name": primary.get("group_name"),
                "phase": primary.get("phase"),
                "phases": primary.get("phases") or [],
                "users_total": primary.get("users_total") or 0,
                "started_at": primary.get("started_at"),
                "updated_at": primary.get("updated_at"),
                "finished_at": primary.get("finished_at"),
                "message": primary.get("message") or "",
                "ok": primary.get("ok") or 0,
                "failed": primary.get("failed") or 0,
            }
        )
    else:
        out.update(
            {
                "kind": None,
                "title": "",
                "group_id": None,
                "group_name": "",
                "phase": None,
                "phases": [],
                "users_total": 0,
                "started_at": None,
                "updated_at": None,
                "finished_at": None,
                "message": "",
                "ok": 0,
                "failed": 0,
            }
        )
    return out


def start_job(
    kind: str,
    title: str,
    *,
    total: int = 0,
    message: str = "",
    group_id: int | None = None,
    group_name: str = "",
    phases: list[dict[str, Any]] | None = None,
) -> str:
    """Register a new background job; returns job_id."""
    now = time.time()
    jid = _new_id()
    job: dict[str, Any] = {
        "id": jid,
        "active": True,
        "kind": str(kind),
        "icon": _KIND_ICON.get(str(kind), "sync"),
        "title": str(title or kind),
        "message": str(message or "در حال اجرا…"),
        "group_id": int(group_id) if group_id is not None else None,
        "group_name": str(group_name or ""),
        "phase": None,
        "phases": phases if phases is not None else [],
        "done": 0,
        "total": max(0, int(total)),
        "ok": 0,
        "failed": 0,
        "users_total": max(0, int(total)),
        "errors": [],
        "started_at": now,
        "updated_at": now,
        "finished_at": None,
    }
    with _lock:
        _prune_locked(now)
        _jobs[jid] = job
    return jid


def update_job(
    job_id: str,
    *,
    message: str | None = None,
    done: int | None = None,
    total: int | None = None,
    ok: int | None = None,
    failed: int | None = None,
    phase: str | None = None,
) -> None:
    if not job_id:
        return
    now = time.time()
    with _lock:
        job = _jobs.get(job_id)
        if not job or not job.get("active"):
            return
        job["updated_at"] = now
        if message is not None:
            job["message"] = message
        if done is not None:
            job["done"] = max(0, int(done))
        if total is not None:
            job["total"] = max(0, int(total))
            job["users_total"] = max(0, int(total))
        if ok is not None:
            job["ok"] = max(0, int(ok))
        if failed is not None:
            job["failed"] = max(0, int(failed))
        if phase is not None:
            job["phase"] = phase


def bump_job(
    job_id: str,
    *,
    ok: bool = True,
    n: int = 1,
    message: str | None = None,
) -> None:
    if not job_id:
        return
    now = time.time()
    step = max(1, int(n))
    with _lock:
        job = _jobs.get(job_id)
        if not job or not job.get("active"):
            return
        job["updated_at"] = now
        job["done"] = int(job.get("done") or 0) + step
        if ok:
            job["ok"] = int(job.get("ok") or 0) + step
        else:
            job["failed"] = int(job.get("failed") or 0) + step
        if message is not None:
            job["message"] = message


def finish_job_id(
    job_id: str,
    *,
    ok: int | None = None,
    failed: int | None = None,
    message: str = "تمام شد",
) -> None:
    if not job_id:
        return
    now = time.time()
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job["active"] = False
        job["phase"] = "done"
        if ok is not None:
            job["ok"] = int(ok)
        if failed is not None:
            job["failed"] = int(failed)
        job["message"] = message
        job["updated_at"] = now
        job["finished_at"] = now
        for ph in job.get("phases") or []:
            if ph.get("status") == "running":
                ph["status"] = "done"
            elif ph.get("status") == "pending":
                ph["status"] = "skipped"
        _prune_locked(now)


# ── Group resync helpers (phased) ──────────────────────────────────────────


def start_group_resync(
    *,
    group_id: int,
    group_name: str = "",
    users_total: int = 0,
) -> str:
    phases = _empty_phases(total=users_total)
    jid = start_job(
        "group_resync",
        f"اعمال پنل‌های گروه «{group_name or group_id}» روی ساب",
        total=users_total,
        message="شروع همگام‌سازی…",
        group_id=int(group_id),
        group_name=str(group_name or ""),
        phases=phases,
    )
    with _lock:
        _group_jobs[int(group_id)] = jid
    return jid


def finish_group_job(
    group_id: int,
    *,
    ok: int = 0,
    failed: int = 0,
    message: str = "تمام شد",
) -> None:
    with _lock:
        jid = _group_jobs.pop(int(group_id), None)
    if jid:
        finish_job_id(jid, ok=ok, failed=failed, message=message)


def set_phase(
    job_id: str,
    phase_id: str,
    *,
    status: str | None = None,
    total: int | None = None,
    message: str | None = None,
) -> None:
    if not job_id:
        return
    now = time.time()
    with _lock:
        job = _jobs.get(job_id)
        if not job or not job.get("active"):
            return
        job["updated_at"] = now
        job["phase"] = phase_id
        if message is not None:
            job["message"] = message
        for ph in job.get("phases") or []:
            if ph.get("id") != phase_id:
                continue
            if status is not None:
                ph["status"] = status
            if total is not None:
                ph["total"] = max(0, int(total))
            break


def note_job_error(job_id: str, text: str, *, limit: int = 8) -> None:
    """Append a short human-readable failure line for the dashboard card."""
    if not job_id:
        return
    msg = " ".join(str(text or "").split()).strip()
    if not msg:
        return
    if len(msg) > 220:
        msg = msg[:217] + "…"
    now = time.time()
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        errs = list(job.get("errors") or [])
        if msg in errs:
            job["updated_at"] = now
            return
        errs.append(msg)
        job["errors"] = errs[-max(1, int(limit)) :]
        job["updated_at"] = now


def bump_phase(
    job_id: str,
    phase_id: str,
    *,
    ok: bool = True,
    n: int = 1,
) -> None:
    if not job_id:
        return
    now = time.time()
    step = max(1, int(n))
    with _lock:
        job = _jobs.get(job_id)
        if not job or not job.get("active"):
            return
        job["updated_at"] = now
        job["done"] = int(job.get("done") or 0) + step
        if ok:
            job["ok"] = int(job.get("ok") or 0) + step
        else:
            job["failed"] = int(job.get("failed") or 0) + step
        for ph in job.get("phases") or []:
            if ph.get("id") != phase_id:
                continue
            ph["done"] = int(ph.get("done") or 0) + step
            if ok:
                ph["ok"] = int(ph.get("ok") or 0) + step
            else:
                ph["failed"] = int(ph.get("failed") or 0) + step
            break


def finish_phase(
    job_id: str,
    phase_id: str,
    *,
    status: str = "done",
    message: str | None = None,
) -> None:
    if not job_id:
        return
    now = time.time()
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job["updated_at"] = now
        if message is not None:
            job["message"] = message
        for ph in job.get("phases") or []:
            if ph.get("id") != phase_id:
                continue
            ph["status"] = status
            total = int(ph.get("total") or 0)
            if total > 0 and status == "done":
                ph["done"] = max(int(ph.get("done") or 0), total)
            break


def finish_job(
    job_id: str | None = None,
    *,
    ok: int = 0,
    failed: int = 0,
    message: str = "تمام شد",
) -> None:
    """Finish a job by id. Legacy keyword-only callers must pass job_id first."""
    if job_id:
        finish_job_id(job_id, ok=ok, failed=failed, message=message)


def has_active_kind(*kinds: str) -> bool:
    want = {str(k) for k in kinds if k}
    if not want:
        return False
    with _lock:
        for job in _jobs.values():
            if job.get("active") and str(job.get("kind") or "") in want:
                return True
    return False


def start_bulk_retry(
    *,
    users_total: int = 0,
    title: str = "رفع خطاهای پنل",
    kind: str = "bulk_retry",
) -> str | None:
    """One clean card — no nested phase (avoids «۲ کار / ۴ آیتم»).

    Returns None if a retry job is already active (dedupe auto+manual).
    ``users_total`` is informational only; we do not fake 0/N progress.
    """
    if has_active_kind("bulk_retry", "auto_retry"):
        return None
    msg = "در حال تلاش مجدد برای اکانت‌های خطادار…"
    if users_total > 0:
        msg = f"در حال تلاش مجدد — حدود {int(users_total)} اکانت خطادار"
    return start_job(
        kind,
        title,
        total=0,  # unknown until finished — indeterminate bar
        message=msg,
        phases=[],  # single card, no nested «تلاش مجدد» row
    )


def kind_label(kind: str | None) -> str:
    return {
        "group_resync": "اعمال پنل گروه",
        "category_resync": "همگام‌سازی دسته",
        "category_move": "جابه‌جایی گروه",
        "bulk_retry": "رفع خطا (دستی)",
        "auto_retry": "رفع خطا (خودکار)",
        "heal_panel": "ترمیم پنل",
        "heal_coverage": "ترمیم پوشش",
        "provision": "اضافه به ساب",
        "sync_modify": "همگام‌سازی پنل",
        "delete_remotes": "حذف از پنل",
        "recount": "بازشماری",
    }.get(str(kind or ""), "پس‌زمینه")
