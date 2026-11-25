"""FastAPI agent that executes panel adapter ops for the Astra master."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("astra.worker.agent")

MASTER_URL = (os.environ.get("ASTRA_MASTER_URL") or "").rstrip("/")
WORKER_TOKEN = (os.environ.get("ASTRA_WORKER_TOKEN") or "").strip()
WORKER_NAME = os.environ.get("ASTRA_WORKER_NAME") or "astra-worker"
WORKER_VERSION = "1.0.31"

# Assigned panels for background usage collection (exact lease from Master)
_ASSIGNED: list[dict[str, Any]] = []
_TIERS: dict[int, dict[str, list[str]]] = {}  # panel_id → {hot, warm}
_INTERVAL = 20  # Warm-tier staggered waves
_COLD_EVERY_SEC = 60.0  # Cold full-panel sweep cadence (independent of priority)
_COLLECT_CONC = 4  # warm Sem(4); cold uses separate Sem(2)
_COLD_CONC = 2  # cold must not occupy all collect slots for 60s walls
_REPORT_CONC = 2
_METER_CHUNK = 3000
_LAST_MAP: dict[int, dict[str, int]] = {}  # panel_id → last sent meters
_LAST_MAP_TS: dict[int, float] = {}  # panel_id → last meter map update (mono)
_LAST_COLD_TS: dict[int, float] = {}  # panel_id → last full sweep monotonic
_METER_CACHE_MAX_AGE = 15.0  # serve get_usage from cache when fresher than this
_COLLECT_TASK: asyncio.Task | None = None
_PRIORITY_TASK: asyncio.Task | None = None
_COMMANDS_TASK: asyncio.Task | None = None
_ASSIGN_TASK: asyncio.Task | None = None
_HEARTBEAT_TASK: asyncio.Task | None = None
_CACHE_SYNC_TASK: asyncio.Task | None = None
# Separate report gates — priority hot posts must never stall warm/cold waves.
_REPORT_SEM: asyncio.Semaphore | None = None  # hot / priority-probe
_COLLECT_REPORT_SEM: asyncio.Semaphore | None = None  # warm + cold
_COLLECT_PANEL_SEM: asyncio.Semaphore | None = None  # warm Sem(4)
_COLD_PANEL_SEM: asyncio.Semaphore | None = None  # cold Sem(2)
# Separate clients so KILL_USER / cold sweeps never stall priority-probe.
_HTTP_PRIORITY: httpx.AsyncClient | None = None
_HTTP_COLLECT: httpx.AsyncClient | None = None
_HTTP_CMDS: httpx.AsyncClient | None = None
# Per-panel circuit breaker: 3 consecutive 5xx → 30s cooldown
_PANEL_FAILS: dict[int, int] = {}
_PANEL_CIRCUIT: dict[int, float] = {}  # open-until monotonic
_PANEL_FAIL_TRIP = 3
# KILL_USER retry queue: (due_mono, attempt, account_dict)
_KILL_RETRY: list[tuple[float, int, dict[str, Any]]] = []
# Parked kills awaiting 5-minute re-sync: (panel_id, username_lower) → account
_KILL_PARKED: dict[tuple[int, str], dict[str, Any]] = {}
_KILL_RESYNC_EVERY = 300.0
_LAST_KILL_RESYNC = 0.0
_KILL_MAX_ATTEMPTS = 3
_KILL_BACKOFF_SEC = (3.0, 10.0, 25.0)  # before retry 1 / 2 / 3
# Per-panel disable fan-out — never hammer a busy remote with >3 concurrent PUTs
_KILL_PANEL_SEMS: dict[int, asyncio.Semaphore] = {}
# Per-panel live get_usage — one slow tunnel must not starve siblings.
_USAGE_PANEL_SEMS: dict[int, asyncio.Semaphore] = {}
_USAGE_PANEL_SEM_LIMIT = 4
# Per-panel 5xx storm breaker for KILL_USER (5 strikes → 60s cooldown)
_KILL_5XX: dict[int, int] = {}
_KILL_COOLDOWN: dict[int, float] = {}
# get_usage / disable_user 404 negative cache: (panel_id, username_lower) → expiry mono
_USAGE_404: dict[tuple[int, str], float] = {}
_USAGE_404_TTL = 900.0  # 15 minutes — never re-query missing remotes
_PANEL_COLLECT_TIMEOUT = 8.0  # warm hard wall — free slots fast on tunnel stalls
_PANEL_PRIO_TIMEOUT = 4.0  # Hot / priority-probe per-panel hard wall
_PANEL_COLD_TIMEOUT = 60.0  # cold full-panel wall; resume via _COLD_OFFSET
_COLD_OFFSET: dict[int, int] = {}  # panel_id → next list offset (checkpoint)
_HOT_RR = 0  # round-robin cursor over local Hot tier fallback
_PRIO_FETCH_LIMIT = 400
_PRIO_PANEL_SEM = 24  # all Hot panels in one wave so tick stays ≤5s
_PRIO_TICK_TIMEOUT = 4.8  # hard wall for the whole priority gather (Hot ≤4s/panel)
_BULK_NAME_THRESHOLD = 5  # >5 targets → bulk /api/users list + in-memory filter
# Create/modify must not starve behind warm/cold HTTP storms on the same panels.
_CREATE_INFLIGHT = 0
_CREATE_SEM = asyncio.Semaphore(28)
_MUTATE_OPS = frozenset({"create_user", "modify_user", "delete_user"})

app = FastAPI(title="Astra Worker", version=WORKER_VERSION)


def _new_http(*, max_conn: int = 20, timeout: float = 60.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        # 6s connect / read capped by per-call wait_for — keep pool warm.
        timeout=httpx.Timeout(timeout, connect=6.0),
        verify=False,
        limits=httpx.Limits(
            max_connections=max_conn,
            max_keepalive_connections=max(4, max_conn // 2),
            keepalive_expiry=90.0,
        ),
    )


async def _http_priority() -> httpx.AsyncClient:
    global _HTTP_PRIORITY
    if _HTTP_PRIORITY is None or _HTTP_PRIORITY.is_closed:
        _HTTP_PRIORITY = _new_http(max_conn=32, timeout=30.0)
    return _HTTP_PRIORITY


async def _http_collect() -> httpx.AsyncClient:
    global _HTTP_COLLECT
    if _HTTP_COLLECT is None or _HTTP_COLLECT.is_closed:
        _HTTP_COLLECT = _new_http(max_conn=24, timeout=100.0)
    return _HTTP_COLLECT


async def _http_cmds() -> httpx.AsyncClient:
    global _HTTP_CMDS
    if _HTTP_CMDS is None or _HTTP_CMDS.is_closed:
        _HTTP_CMDS = _new_http(max_conn=12, timeout=25.0)
    return _HTTP_CMDS


def _usage_404_hit(pid: int, uname: str) -> bool:
    key = (int(pid), (uname or "").strip().lower())
    exp = _USAGE_404.get(key)
    if exp is None:
        return False
    if exp < time.monotonic():
        _USAGE_404.pop(key, None)
        return False
    return True


def _usage_404_mark(pid: int, uname: str) -> None:
    key = (int(pid), (uname or "").strip().lower())
    _USAGE_404[key] = time.monotonic() + _USAGE_404_TTL
    if len(_USAGE_404) > 50000:
        now = time.monotonic()
        for k, e in list(_USAGE_404.items()):
            if e < now:
                _USAGE_404.pop(k, None)


def _is_404_err(err: object) -> bool:
    if err is None:
        return False
    code = getattr(err, "status_code", None) or getattr(err, "status", None)
    try:
        if int(code or 0) == 404:
            return True
    except (TypeError, ValueError):
        pass
    resp = getattr(err, "response", None)
    if resp is not None:
        try:
            if int(getattr(resp, "status_code", 0) or 0) == 404:
                return True
        except (TypeError, ValueError):
            pass
    s = str(err or "").lower()
    return (
        "404" in s
        or "not found" in s
        or "does not exist" in s
        or "user not found" in s
        or "no user" in s
        or "پیدا نشد" in str(err or "")
    )


def _result_is_404(res: object) -> bool:
    if res is None:
        return False
    if _is_404_err(getattr(res, "error", None)):
        return True
    try:
        if int(getattr(res, "status_code", 0) or 0) == 404:
            return True
    except (TypeError, ValueError):
        pass
    return False


def _hot_tier_slice(limit: int = 400) -> list[tuple[int, str]]:
    """Round-robin Hot remotes from assignment tiers (fallback if Master queue empty)."""
    global _HOT_RR
    pairs: list[tuple[int, str]] = []
    for pid, tier in (_TIERS or {}).items():
        for name in list(tier.get("hot") or []):
            n = str(name or "").strip()
            if n:
                pairs.append((int(pid), n))
    if not pairs:
        return []
    lim = max(1, min(500, int(limit)))
    start = int(_HOT_RR) % len(pairs)
    _HOT_RR = start + lim
    out = pairs[start : start + lim]
    if len(out) < lim:
        out = out + pairs[: lim - len(out)]
    return out


def _is_5xx_err(err: object) -> bool:
    code = getattr(err, "status_code", None) or getattr(err, "status", None)
    try:
        if int(code or 0) in (500, 502, 503, 504):
            return True
    except (TypeError, ValueError):
        pass
    resp = getattr(err, "response", None)
    if resp is not None:
        try:
            if int(getattr(resp, "status_code", 0) or 0) in (500, 502, 503, 504):
                return True
        except (TypeError, ValueError):
            pass
    s = str(err or "").lower()
    return (
        " 500" in f" {s}"
        or "http 500" in s
        or "502" in s
        or "503" in s
        or "504" in s
        or "internal server" in s
        or "bad gateway" in s
        or "gateway timeout" in s
    )


def _kill_panel_blocked(pid: int) -> bool:
    until = float(_KILL_COOLDOWN.get(int(pid), 0.0) or 0.0)
    return time.monotonic() < until


def _kill_note_5xx(pid: int) -> None:
    pid = int(pid)
    if pid <= 0:
        return
    n = int(_KILL_5XX.get(pid, 0) or 0) + 1
    _KILL_5XX[pid] = n
    if n >= 5:
        _KILL_COOLDOWN[pid] = time.monotonic() + 60.0
        _KILL_5XX[pid] = 0
        logger.warning(
            "kill circuit OPEN panel=%s for 60s (5x 5xx)", pid
        )
    # Fast-trip when a single KILL_USER fan-out hammers a dying panel.
    elif n >= 3:
        _KILL_COOLDOWN[pid] = time.monotonic() + 60.0
        logger.warning(
            "kill circuit OPEN panel=%s for 60s (3x 5xx burst)", pid
        )
        _KILL_5XX[pid] = 0


def _kill_note_ok(pid: int) -> None:
    _KILL_5XX.pop(int(pid), None)


class ExecBody(BaseModel):
    op: str
    panel: dict[str, Any]
    args: dict[str, Any] = Field(default_factory=dict)


class AssignBody(BaseModel):
    master_url: str | None = None
    interval_sec: int = 30
    panels: list[dict[str, Any]] = Field(default_factory=list)


def _require_token(authorization: str | None = Header(default=None)) -> None:
    raw = (authorization or "").strip()
    token = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
    if not WORKER_TOKEN or token != WORKER_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _build_adapter(panel: dict[str, Any]):
    """Instantiate the same adapters as master (no ORM / no worker routing)."""
    from app.adapters.guard import GuardAdapter
    from app.adapters.marzban import MarzbanAdapter
    from app.adapters.pasarguard import PasarGuardAdapter
    from app.adapters.rebecca import RebeccaAdapter
    from app.adapters.sanaei import SanaeiAdapter

    ptype = str(panel.get("type") or "").strip().lower()
    meta = dict(panel.get("meta") or {})
    if panel.get("id") is not None:
        meta["panel_id"] = int(panel["id"])
    kwargs = dict(
        base_url=str(panel.get("base_url") or ""),
        username=panel.get("username"),
        password=panel.get("password"),
        api_token=panel.get("api_token"),
        meta=meta,
    )
    if ptype in ("pasarguard", "pasar_guard"):
        return PasarGuardAdapter(**kwargs)
    if ptype == "marzban":
        return MarzbanAdapter(**kwargs)
    if ptype == "rebecca":
        return RebeccaAdapter(**kwargs)
    if ptype == "sanaei":
        return SanaeiAdapter(**kwargs)
    if ptype == "guard":
        return GuardAdapter(**kwargs)
    raise HTTPException(
        status_code=400, detail=f"unsupported panel type on worker: {ptype}"
    )


def _payload_from(args: dict[str, Any]):
    from app.adapters.base import UserPayload

    raw = args.get("payload") or args
    return UserPayload(
        username=str(raw.get("username") or ""),
        data_limit=int(raw.get("data_limit") or 0),
        expire=raw.get("expire"),
        status=str(raw.get("status") or "active"),
        note=raw.get("note"),
        uuid=raw.get("uuid"),
        config=dict(raw.get("config") or {}),
    )


def _result_json(res: Any) -> dict[str, Any]:
    return {
        "success": bool(getattr(res, "success", False)),
        "remote_username": getattr(res, "remote_username", None),
        "remote_id": getattr(res, "remote_id", None),
        "uuid": getattr(res, "uuid", None),
        "links": list(getattr(res, "links", None) or []),
        "used_traffic": int(getattr(res, "used_traffic", 0) or 0),
        "error": getattr(res, "error", None),
        "raw": getattr(res, "raw", None),
        "remote_status": getattr(res, "remote_status", None),
        "remote_expire": getattr(res, "remote_expire", None),
        "online_at": getattr(res, "online_at", None),
    }


@app.get("/health")
async def health(_auth: None = Depends(_require_token)):
    return {
        "ok": True,
        "name": WORKER_NAME,
        "version": WORKER_VERSION,
        "assigned": len(_ASSIGNED),
        "interval": _INTERVAL,
    }


@app.post("/v1/exec")
async def exec_op(body: ExecBody, _auth: None = Depends(_require_token)):
    global _CREATE_INFLIGHT
    adapter = _build_adapter(body.panel)
    op = (body.op or "").strip()
    args = body.args or {}
    try:
        if op == "test_connection":
            res = await adapter.test_connection()
        elif op in _MUTATE_OPS:
            # Bounded fan-out + wall so create never hangs forever behind collect.
            from types import SimpleNamespace

            async with _CREATE_SEM:
                _CREATE_INFLIGHT += 1
                try:
                    if op == "create_user":
                        res = await asyncio.wait_for(
                            adapter.create_user(_payload_from(args)),
                            timeout=35.0,
                        )
                    elif op == "modify_user":
                        res = await asyncio.wait_for(
                            adapter.modify_user(_payload_from(args)),
                            timeout=30.0,
                        )
                    else:
                        res = await asyncio.wait_for(
                            adapter.delete_user(
                                str(args.get("username") or ""),
                                args.get("remote_id"),
                            ),
                            timeout=25.0,
                        )
                except asyncio.TimeoutError:
                    res = SimpleNamespace(
                        success=False,
                        error="Timeout waiting for panel response",
                        used_traffic=0,
                        remote_username=None,
                        remote_id=None,
                        links=[],
                        raw=None,
                        remote_status=None,
                        remote_expire=None,
                        online_at=None,
                        uuid=None,
                    )
                finally:
                    _CREATE_INFLIGHT = max(0, _CREATE_INFLIGHT - 1)
        elif op == "get_usage":
            # Exact remote only — never case-variants / /sub scrape on meter path.
            uname = str(args.get("username") or "").strip()
            rid = args.get("remote_id")
            expand = bool(args.get("expand_links", False))
            bypass_cache = bool(args.get("bypass_cache", False))
            pid = int((body.panel or {}).get("id") or 0)
            cached = (
                None
                if bypass_cache
                else (_cached_usage(pid, uname) if pid and uname else None)
            )
            if cached is not None and not expand:
                from types import SimpleNamespace

                res = SimpleNamespace(
                    success=True,
                    error=None,
                    used_traffic=int(cached),
                    remote_username=uname,
                    remote_id=rid,
                    links=[],
                    raw={"source": "meter_cache", "age_lt_sec": _METER_CACHE_MAX_AGE},
                    remote_status=None,
                    remote_expire=None,
                    online_at=None,
                    uuid=None,
                )
            elif pid and _circuit_open(pid):
                from types import SimpleNamespace

                res = SimpleNamespace(
                    success=False,
                    error="circuit-open",
                    used_traffic=0,
                    remote_username=uname,
                    remote_id=rid,
                    links=[],
                    raw=None,
                    remote_status=None,
                    remote_expire=None,
                    online_at=None,
                    uuid=None,
                )
            elif pid and uname and _usage_404_hit(pid, uname):
                from types import SimpleNamespace

                res = SimpleNamespace(
                    success=False,
                    error="HTTP 404 Not Found (cached)",
                    used_traffic=0,
                    remote_username=uname,
                    remote_id=rid,
                    links=[],
                    raw=None,
                    remote_status=None,
                    remote_expire=None,
                    online_at=None,
                    uuid=None,
                )
            else:
                sem = _USAGE_PANEL_SEMS.get(pid)
                if sem is None and pid:
                    sem = asyncio.Semaphore(_USAGE_PANEL_SEM_LIMIT)
                    _USAGE_PANEL_SEMS[pid] = sem

                async def _live_get_usage():
                    try:
                        # Prefer exact username; remote_id only as path hint, never
                        # triggers lowercase/uppercase or /sub fallbacks.
                        # Bound live upstream: 6s connect / 10s read via wait_for.
                        return await asyncio.wait_for(
                            adapter.get_usage(
                                uname, rid, expand_links=bool(expand)
                            ),
                            timeout=10.0,
                        )
                    except TypeError:
                        return await asyncio.wait_for(
                            adapter.get_usage(uname, rid), timeout=10.0
                        )

                try:
                    if sem is not None:
                        async with sem:
                            res = await _live_get_usage()
                    else:
                        res = await _live_get_usage()
                except Exception as exc:  # noqa: BLE001
                    if pid and uname and _is_404_err(exc):
                        _usage_404_mark(pid, uname)
                    if pid and _is_5xx_err(exc):
                        _circuit_fail(pid)
                    raise
                if pid and uname and _result_is_404(res):
                    _usage_404_mark(pid, uname)
                elif pid and _result_is_5xx(res):
                    _circuit_fail(pid)
                elif pid:
                    _circuit_ok(pid)
                    # Keep in-memory map fresh for subsequent cache hits.
                    try:
                        used_i = max(0, int(getattr(res, "used_traffic", 0) or 0))
                        umap = dict(_LAST_MAP.get(pid) or {})
                        umap[uname.strip().lower()] = used_i
                        _LAST_MAP[pid] = umap
                        _LAST_MAP_TS[pid] = time.monotonic()
                    except Exception:  # noqa: BLE001
                        pass
        elif op == "get_subscription_links":
            res = await adapter.get_subscription_links(
                str(args.get("username") or ""), args.get("remote_id")
            )
        elif op == "reset_usage":
            res = await adapter.reset_usage(
                str(args.get("username") or ""), args.get("remote_id")
            )
        elif op == "list_users":
            # Optional bulk — adapters expose differently; best-effort via marzban
            list_fn = getattr(adapter, "list_users", None)
            if not callable(list_fn):
                raise HTTPException(status_code=400, detail="list_users unsupported")
            res = await list_fn(**{k: v for k, v in args.items() if k in ("offset", "limit")})
        else:
            raise HTTPException(status_code=400, detail=f"unknown op: {op}")
        return _result_json(res)
    finally:
        close = getattr(adapter, "close", None)
        if callable(close):
            try:
                await close()
            except Exception:  # noqa: BLE001
                pass


@app.post("/v1/assign")
async def assign_panels(body: AssignBody, _auth: None = Depends(_require_token)):
    global _ASSIGNED, _INTERVAL, MASTER_URL, _COLLECT_TASK, _PRIORITY_TASK
    global _COMMANDS_TASK, _ASSIGN_TASK
    if body.master_url:
        MASTER_URL = str(body.master_url).rstrip("/")
        os.environ["ASTRA_MASTER_URL"] = MASTER_URL
    _INTERVAL = max(20, min(300, int(body.interval_sec or 25)))
    _ASSIGNED = list(body.panels or [])
    _ensure_bg_tasks()
    return {"ok": True, "panels": len(_ASSIGNED), "interval": _INTERVAL}


def _ensure_bg_tasks() -> None:
    global _COLLECT_TASK, _PRIORITY_TASK, _COMMANDS_TASK, _ASSIGN_TASK
    global _HEARTBEAT_TASK, _CACHE_SYNC_TASK
    if _COLLECT_TASK is None or _COLLECT_TASK.done():
        _COLLECT_TASK = asyncio.create_task(_collect_loop(), name="usage-collect")
    if _PRIORITY_TASK is None or _PRIORITY_TASK.done():
        _PRIORITY_TASK = asyncio.create_task(
            _priority_loop(), name="usage-priority"
        )
    if _COMMANDS_TASK is None or _COMMANDS_TASK.done():
        _COMMANDS_TASK = asyncio.create_task(
            _commands_loop(), name="worker-commands"
        )
    if _ASSIGN_TASK is None or _ASSIGN_TASK.done():
        _ASSIGN_TASK = asyncio.create_task(
            _assignment_loop(), name="worker-assignment"
        )
    if _HEARTBEAT_TASK is None or _HEARTBEAT_TASK.done():
        _HEARTBEAT_TASK = asyncio.create_task(
            _heartbeat_loop(), name="worker-heartbeat"
        )
    if _CACHE_SYNC_TASK is None or _CACHE_SYNC_TASK.done():
        _CACHE_SYNC_TASK = asyncio.create_task(
            _cache_sync_loop(), name="usage-cache-sync"
        )


class SelfUpdateBody(BaseModel):
    bundle_url: str | None = None
    expected_version: str | None = None


@app.post("/v1/self-update")
async def self_update(
    body: SelfUpdateBody | None = None,
    _auth: None = Depends(_require_token),
):
    """Download worker bundle from master and restart cleanly."""
    import io
    import subprocess
    import tarfile
    from pathlib import Path

    root = Path(os.environ.get("ASTRA_WORKER_ROOT") or "/opt/astra-worker")
    master = MASTER_URL
    if body and body.bundle_url:
        url = str(body.bundle_url).strip()
    else:
        if not master:
            raise HTTPException(status_code=400, detail="master_url unknown")
        url = master.rstrip("/") + "/api/public/worker-bundle.tgz"
    headers = {"Authorization": f"Bearer {WORKER_TOKEN}"} if WORKER_TOKEN else {}
    try:
        async with httpx.AsyncClient(timeout=120.0, verify=False, follow_redirects=True) as client:
            res = await client.get(url, headers=headers)
        if res.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"bundle HTTP {res.status_code}",
            )
        data = res.content
        if len(data) < 100:
            raise HTTPException(status_code=502, detail="bundle too small")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            # Only extract worker/ + app/ (adapters) into install root.
            for member in tar.getmembers():
                name = member.name.lstrip("./")
                if not (
                    name.startswith("worker/")
                    or name.startswith("app/")
                    or name == "requirements.txt"
                ):
                    continue
                # Prevent path escape
                if ".." in Path(name).parts:
                    continue
                tar.extract(member, path=root)
        # worker package layout: /opt/astra-worker/worker/app.py
        # tarball has worker/app.py → extract to root/worker/app.py ✓
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("self-update extract failed")
        raise HTTPException(status_code=500, detail=str(exc)[:200]) from exc

    async def _restart() -> None:
        await asyncio.sleep(1.2)
        try:
            subprocess.Popen(  # noqa: S603
                ["systemctl", "restart", "astra-worker"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("self-update restart spawn failed")

    asyncio.create_task(_restart(), name="worker-self-update-restart")
    return {
        "ok": True,
        "restarting": True,
        "from_version": WORKER_VERSION,
        "expected": (body.expected_version if body else None),
        "bytes": len(data),
    }


async def _heartbeat_once() -> None:
    if not MASTER_URL or not WORKER_TOKEN:
        return
    try:
        client = await _http_priority()
        await client.post(
            f"{MASTER_URL}/api/internal/worker/heartbeat",
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
            json={
                "version": WORKER_VERSION,
                "panels": [int(p.get("id") or 0) for p in _ASSIGNED if p.get("id")],
            },
            timeout=8.0,
        )
    except Exception:  # noqa: BLE001
        logger.debug("heartbeat failed", exc_info=True)


async def _heartbeat_loop() -> None:
    """Dedicated ~3s heartbeat so cold sweeps / long-polls never starve the lease clock."""
    await asyncio.sleep(1.0)
    while True:
        try:
            await _heartbeat_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("heartbeat loop error", exc_info=True)
        await asyncio.sleep(3.0)


def _used_bytes(data: dict[str, Any]) -> int:
    """Period used bytes. Never prefer lifetime when used_traffic is present (incl. 0)."""
    try:
        if "used_traffic" in data:
            v = data.get("used_traffic")
            if v is not None and v != "":
                return max(0, int(v))
        for key in ("current_usage", "total_usage"):
            v = data.get(key)
            if v is not None and int(v) > 0:
                return int(v)
        # Period key missing — last-resort only (never after an explicit 0).
        for key in ("lifetime_used_traffic", "total_traffic"):
            v = data.get(key)
            if v is not None and int(v) > 0:
                return int(v)
    except (TypeError, ValueError):
        pass
    return 0


async def _sanaei_usage_map(adapter: Any) -> dict[str, int]:
    """email → used bytes from one Sanaei inbounds/list."""
    out: dict[str, int] = {}
    try:
        inbounds = await adapter._raw_inbounds_for_usage()
    except Exception:  # noqa: BLE001
        logger.warning("sanaei inbounds list failed", exc_info=True)
        return out
    for inbound in inbounds or []:
        if not isinstance(inbound, dict):
            continue
        stats = inbound.get("clientStats") or []
        hit = False
        if isinstance(stats, list):
            for stat in stats:
                if not isinstance(stat, dict):
                    continue
                email = str(stat.get("email") or "").strip()
                if not email:
                    continue
                hit = True
                got = int(stat.get("up") or 0) + int(stat.get("down") or 0)
                out[email] = int(out.get(email) or 0) + max(0, got)
        if hit:
            continue
        settings_raw = inbound.get("settings")
        settings: Any = settings_raw
        if isinstance(settings_raw, str):
            try:
                import json as _json

                settings = _json.loads(settings_raw)
            except Exception:  # noqa: BLE001
                settings = {}
        if not isinstance(settings, dict):
            continue
        for client in settings.get("clients") or []:
            if not isinstance(client, dict):
                continue
            email = str(client.get("email") or "").strip()
            if not email:
                continue
            got = int(client.get("up") or 0) + int(client.get("down") or 0)
            out[email] = int(out.get(email) or 0) + max(0, got)
    return out


def _circuit_open(pid: int) -> bool:
    return time.monotonic() < float(_PANEL_CIRCUIT.get(int(pid), 0.0) or 0.0)


def _circuit_ok(pid: int) -> None:
    _PANEL_FAILS[int(pid)] = 0


def _circuit_fail(pid: int) -> None:
    pid = int(pid)
    n = int(_PANEL_FAILS.get(pid, 0) or 0) + 1
    _PANEL_FAILS[pid] = n
    if n >= int(_PANEL_FAIL_TRIP):
        _PANEL_CIRCUIT[pid] = time.monotonic() + 30.0
        _PANEL_FAILS[pid] = 0
        logger.warning(
            "panel circuit OPEN panel=%s for 30s after %sx 5xx",
            pid,
            _PANEL_FAIL_TRIP,
        )


def _is_5xx_err(exc: BaseException | str | None) -> bool:
    text = str(exc or "").lower()
    if "http 5" in text or "status code 5" in text:
        return True
    for code in ("500", "502", "503", "504"):
        if f"http {code}" in text or f" {code} " in text:
            return True
    return False


def _result_is_5xx(res: Any) -> bool:
    err = str(getattr(res, "error", "") or "")
    return _is_5xx_err(err)


async def _get_usage_exact(
    adapter: Any,
    username: str,
    *,
    panel_id: int = 0,
    timeout: float = 2.5,
) -> Any | None:
    """Single exact-username get_usage — no case variants, no /sub expand.

    Returns the adapter result, or ``None`` on hard failure. Trips the per-panel
    5xx circuit after ``_PANEL_FAIL_TRIP`` consecutive errors.
    """
    uname = (username or "").strip()
    if not uname:
        return None
    pid = int(panel_id or 0)
    if pid and _usage_404_hit(pid, uname):
        return None
    if pid and _circuit_open(pid):
        return None
    try:
        try:
            r = await asyncio.wait_for(
                adapter.get_usage(uname, None, expand_links=False),
                timeout=float(timeout),
            )
        except TypeError:
            r = await asyncio.wait_for(
                adapter.get_usage(uname, None),
                timeout=float(timeout),
            )
    except Exception as exc:  # noqa: BLE001
        if pid and _is_404_err(exc):
            _usage_404_mark(pid, uname)
            return None
        if pid and _is_5xx_err(exc):
            _circuit_fail(pid)
        return None
    if r is not None and not getattr(r, "success", True):
        if _result_is_404(r):
            if pid:
                _usage_404_mark(pid, uname)
            return None
        if pid and _result_is_5xx(r):
            _circuit_fail(pid)
            return None
    if pid:
        _circuit_ok(pid)
    return r


async def _list_usage_map(
    adapter: Any,
    *,
    start_offset: int = 0,
    max_pages: int = 250,
    deadline: float | None = None,
) -> tuple[dict[str, int], int, bool]:
    """Bulk usage map with checkpoint resume.

    Returns ``(usage_map, next_offset, complete)``. When ``deadline`` is hit
    mid-pagination, ``complete=False`` and ``next_offset`` is where the next
    cold wave should resume (never restarts from 0 starving the tail).
    """
    from app.adapters.marzban import MarzbanAdapter
    from app.adapters.sanaei import SanaeiAdapter

    if isinstance(adapter, SanaeiAdapter):
        return await _sanaei_usage_map(adapter), 0, True
    if not isinstance(adapter, MarzbanAdapter):
        return {}, int(start_offset or 0), True
    out: dict[str, int] = {}
    client, headers = await adapter._authed()
    # Per-page timeout sized for Cold 60s wall + limit=1000 bulk pages.
    timeout = httpx.Timeout(12.0, connect=4.0)
    offset = max(0, int(start_offset or 0))
    page_size = 1000
    for _ in range(max(1, int(max_pages))):
        if deadline is not None and time.monotonic() >= float(deadline):
            return out, offset, False
        try:
            resp = await client.get(
                "/api/users",
                headers=headers,
                params={"limit": page_size, "offset": offset},
                timeout=timeout,
            )
        except (asyncio.TimeoutError, httpx.TimeoutException):
            return out, offset, False
        if resp.status_code >= 400:
            break
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            break
        users = []
        total_hint = None
        if isinstance(payload, dict):
            users = payload.get("users") or payload.get("items") or []
            for tk in ("total", "total_users", "count"):
                if payload.get(tk) is not None:
                    try:
                        total_hint = int(payload.get(tk))
                    except (TypeError, ValueError):
                        total_hint = None
                    break
        elif isinstance(payload, list):
            users = payload
        if not isinstance(users, list) or not users:
            return out, 0, True
        for u in users:
            if not isinstance(u, dict):
                continue
            used = _used_bytes(u)
            name = str(u.get("username") or "").strip()
            if name:
                # One canonical key only — never also store numeric id twin
                # (that double-applied identical meters on master).
                out[name] = max(int(out.get(name) or 0), used)
                continue
            # Username missing — fall back to numeric/string id once.
            uid = u.get("id")
            if uid is not None and str(uid).strip():
                kid = str(uid).strip()
                out[kid] = max(int(out.get(kid) or 0), used)
        offset += len(users)
        if len(users) < page_size:
            return out, 0, True
        if total_hint is not None and offset >= int(total_hint):
            return out, 0, True
        await asyncio.sleep(0.01)
    return out, offset, False


def _canonicalize_meter_map(usage_map: dict[str, int]) -> dict[str, int]:
    """Drop numeric-id twins when a non-numeric username holds the same bytes.

    PasarGuard/Marzban list payloads used to store both ``username`` and ``id``
    with identical used_traffic — master then applied the same meter twice.
    """
    if not usage_map:
        return {}
    by_used: dict[int, list[str]] = {}
    for name, used in usage_map.items():
        key = str(name).strip().lower()
        if not key:
            continue
        by_used.setdefault(int(used or 0), []).append(key)
    drop: set[str] = set()
    for _used, names in by_used.items():
        if len(names) < 2:
            continue
        non_numeric = [n for n in names if not n.isdigit()]
        numeric = [n for n in names if n.isdigit()]
        if non_numeric and numeric:
            drop.update(numeric)
    return {
        str(n).strip().lower(): int(u or 0)
        for n, u in usage_map.items()
        if str(n).strip() and str(n).strip().lower() not in drop
    }


def _cleanup_meter_cache_duplicates() -> int:
    """One-shot startup cleanup of numeric twin rows in sqlite meter_cache."""
    path = os.environ.get("ASTRA_WORKER_CACHE_DB") or "/var/lib/astra-worker/cache.db"
    if not os.path.isfile(path):
        return 0
    removed = 0
    try:
        import sqlite3

        con = sqlite3.connect(path, timeout=10)
        try:
            rows = con.execute(
                "SELECT panel_id, remote_username, used_traffic FROM meter_cache"
            ).fetchall()
            by_panel: dict[int, dict[str, int]] = {}
            for pid, name, used in rows:
                by_panel.setdefault(int(pid), {})[str(name)] = int(used or 0)
            for pid, umap in by_panel.items():
                clean = _canonicalize_meter_map(umap)
                for name in list(umap.keys()):
                    key = str(name).strip().lower()
                    if key and key not in clean:
                        con.execute(
                            "DELETE FROM meter_cache WHERE panel_id=? AND "
                            "lower(remote_username)=?",
                            (int(pid), key),
                        )
                        removed += 1
            con.commit()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        logger.debug("meter_cache cleanup soft-fail", exc_info=True)
    return removed


def _cached_usage(pid: int, username: str) -> int | None:
    """Return cached used_traffic when the panel map is fresher than 15s."""
    pid = int(pid or 0)
    uname = (username or "").strip().lower()
    if not pid or not uname:
        return None
    ts = float(_LAST_MAP_TS.get(pid, 0.0) or 0.0)
    if ts <= 0 or (time.monotonic() - ts) > float(_METER_CACHE_MAX_AGE):
        return None
    umap = _LAST_MAP.get(pid)
    if not umap:
        return None
    if uname in umap:
        return max(0, int(umap.get(uname) or 0))
    return None


def _load_prev_meters(pid: int) -> dict[str, int] | None:
    """In-memory first; fall back to sqlite meter_cache after restart."""
    prev = _LAST_MAP.get(pid)
    if prev is not None:
        return prev
    path = os.environ.get("ASTRA_WORKER_CACHE_DB") or "/var/lib/astra-worker/cache.db"
    if not os.path.isfile(path):
        return None
    try:
        import sqlite3

        con = sqlite3.connect(path, timeout=5)
        try:
            rows = con.execute(
                "SELECT remote_username, used_traffic FROM meter_cache "
                "WHERE panel_id = ?",
                (int(pid),),
            ).fetchall()
        finally:
            con.close()
        if not rows:
            return None
        return {str(n): int(u or 0) for n, u in rows}
    except Exception:  # noqa: BLE001
        return None


def _persist_meters(
    pid: int, usage_map: dict[str, int], *, replace: bool = False
) -> None:
    """Best-effort sqlite write so cold waves stay delta-only across restarts."""
    path = os.environ.get("ASTRA_WORKER_CACHE_DB") or "/var/lib/astra-worker/cache.db"
    try:
        import sqlite3
        from datetime import datetime, timezone

        os.makedirs(os.path.dirname(path), exist_ok=True)
        con = sqlite3.connect(path, timeout=5)
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS meter_cache ("
                "panel_id INT NOT NULL, remote_username TEXT NOT NULL, "
                "used_traffic BIGINT NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, "
                "PRIMARY KEY(panel_id, remote_username))"
            )
            now = datetime.now(timezone.utc).isoformat()
            if replace:
                con.execute(
                    "DELETE FROM meter_cache WHERE panel_id = ?", (int(pid),)
                )
            con.executemany(
                "INSERT OR REPLACE INTO meter_cache "
                "(panel_id, remote_username, used_traffic, updated_at) "
                "VALUES (?, ?, ?, ?)",
                [
                    (int(pid), str(name), int(used or 0), now)
                    for name, used in usage_map.items()
                ],
            )
            con.commit()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        pass


def _delta_meters(
    pid: int,
    usage_map: dict[str, int],
    *,
    force_full: bool = False,
    replace_map: bool = False,
) -> list[dict[str, int | str]]:
    """Send ONLY changed meters — including during Cold / full-sweep waves.

    Always merge into the remembered map (warm polls are partial). Cold
    ``replace_map=True`` drops names that vanished from the remote list.
    Sqlite ``meter_cache`` survives Worker restarts so we never re-blast
    12k unchanged meters after a restart.
    """
    _ = force_full
    # Normalize keys so warm get_usage vs cold list_users casing cannot
    # re-fire the same meter every wave.
    norm_map = _canonicalize_meter_map(
        {
            str(n).strip().lower(): int(u or 0)
            for n, u in usage_map.items()
            if str(n).strip()
        }
    )
    prev_raw = _load_prev_meters(pid) or {}
    prev = _canonicalize_meter_map(
        {
            str(n).strip().lower(): int(u or 0)
            for n, u in prev_raw.items()
            if str(n).strip()
        }
    )
    if not prev:
        # Seed local cache, but still return a rotating sync-touch slice so
        # master last_sync_at moves after worker restart (fleet was 98% stale).
        _LAST_MAP[pid] = dict(norm_map)
        _LAST_MAP_TS[pid] = time.monotonic()
        _persist_meters(pid, norm_map, replace=True)
        if not replace_map and not force_full:
            return []
        wave = int(time.time() // 60)
        seed_items = [
            (name, used)
            for name, used in norm_map.items()
            if (hash(name) + wave) % 3 == 0
        ]
        return [
            {"remote_username": str(n), "used_traffic": int(u or 0)}
            for n, u in seed_items
        ]
    items = [
        (name, used)
        for name, used in norm_map.items()
        if int(prev.get(name, -1)) != int(used)
    ]
    if replace_map or force_full:
        for name, old in prev.items():
            if name not in norm_map and int(old or 0) > 0:
                items.append((name, 0))
        # Cold/full: re-send ~33% of unchanged meters each minute so fleet
        # last_sync_at recovers within ~3 cold cycles without a full blast.
        wave = int(time.time() // 60)
        for name, used in norm_map.items():
            if int(prev.get(name, -1)) != int(used):
                continue
            if (hash(name) + wave) % 3 == 0:
                items.append((name, used))
    out: dict[str, int] = {}
    for name, used in items:
        out[str(name)] = int(used or 0)
    if replace_map:
        new_map = dict(norm_map)
    else:
        new_map = dict(prev)
        new_map.update(norm_map)
        new_map = _canonicalize_meter_map(new_map)
    _LAST_MAP[pid] = new_map
    _LAST_MAP_TS[pid] = time.monotonic()
    # Persist full map on cold; on warm only upsert the observed names.
    _persist_meters(
        pid,
        new_map if replace_map else dict(norm_map),
        replace=replace_map,
    )
    return [
        {"remote_username": name, "used_traffic": used}
        for name, used in out.items()
    ]


async def _cache_sync_loop() -> None:
    """Independent freshness loop — POST rotating meter_cache slices every ~45s.

    Live collect often times out on tunnels; this still proves known remotes to
    master so ``last_sync_at`` (and cold panel stamps) recover fleet-wide.
    """
    await asyncio.sleep(8)
    rr = 0
    while True:
        try:
            panels = [p for p in list(_ASSIGNED) if p.get("id")]
            if panels:
                # ~4 panels per tick → full fleet coverage in a few minutes.
                n = max(3, min(6, len(panels)))
                batch = []
                for _ in range(n):
                    batch.append(panels[rr % len(panels)])
                    rr += 1
                for p in batch:
                    pid = int(p.get("id") or 0)
                    if not pid:
                        continue
                    try:
                        sent = await _post_cache_sync_touch(
                            pid, mode="cold", limit=1200
                        )
                        if sent:
                            logger.info(
                                "cache sync-loop panel=%s sent=%s", pid, sent
                            )
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            "cache sync-loop fail panel=%s", pid, exc_info=True
                        )
                    await asyncio.sleep(0.4)
        except Exception:  # noqa: BLE001
            logger.debug("cache sync-loop wave failed", exc_info=True)
        await asyncio.sleep(45.0)


async def _post_cache_sync_touch(
    pid: int, *, mode: str = "warm", limit: int = 2500
) -> int:
    """POST a rotating meter_cache slice so master can stamp last_sync_at.

    Used when live panel fetch times out / returns empty — cache still proves
    the remotes we already know, unblocking fleet freshness recovery.
    """
    prev = _load_prev_meters(pid) or {}
    if not prev:
        return 0
    wave = int(time.time() // 45)
    items: list[dict[str, int | str]] = []
    for name, used in prev.items():
        key = str(name).strip().lower()
        if not key:
            continue
        if (hash(key) + wave) % 2 != 0:
            continue
        items.append({"remote_username": key, "used_traffic": int(used or 0)})
        if len(items) >= int(limit):
            break
    if not items:
        # Fallback: first N keys if hash slice empty
        for name, used in list(prev.items())[:limit]:
            key = str(name).strip().lower()
            if key:
                items.append(
                    {"remote_username": key, "used_traffic": int(used or 0)}
                )
    if not items:
        return 0
    return await _post_meter_chunks(
        pid=pid,
        meters=items,
        listed=len(prev),
        dt_ms=0,
        full_sync=True,
        mode=mode,
    )


async def _post_meter_chunks(
    *,
    pid: int,
    meters: list[dict[str, int | str]],
    listed: int,
    dt_ms: int,
    full_sync: bool = False,
    mode: str | None = None,
) -> int:
    """POST meters to master in gzip chunks over a keep-alive client."""
    import gzip
    import json

    global _REPORT_SEM, _COLLECT_REPORT_SEM
    is_hot = (mode or "").strip().lower() == "hot"
    if is_hot:
        if _REPORT_SEM is None:
            _REPORT_SEM = asyncio.Semaphore(max(1, int(_REPORT_CONC)))
        report_sem = _REPORT_SEM
    else:
        # Dedicated collect report gate — never waits behind priority-probe.
        if _COLLECT_REPORT_SEM is None:
            _COLLECT_REPORT_SEM = asyncio.Semaphore(4)
        report_sem = _COLLECT_REPORT_SEM
    if not meters:
        return 0
    ok_n = 0
    chunk = max(200, int(_METER_CHUNK))
    # Hot path uses the priority HTTP pool so cold waves never stall /sub.
    client = await _http_priority() if is_hot else await _http_collect()
    async with report_sem:
        for i in range(0, len(meters), chunk):
            part = meters[i : i + chunk]
            payload: dict[str, Any] = {
                "panel_id": pid,
                "meters": part,
                "listed": listed,
                "dt_ms": dt_ms,
                "full_sync": bool(full_sync),
            }
            if mode:
                payload["mode"] = str(mode)
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            body = gzip.compress(raw, compresslevel=6)
            for attempt in range(3):
                try:
                    res = await client.post(
                        f"{MASTER_URL}/api/internal/worker/usage-report",
                        headers={
                            "Authorization": f"Bearer {WORKER_TOKEN}",
                            "Content-Type": "application/json",
                            "Content-Encoding": "gzip",
                        },
                        content=body,
                    )
                    if res.status_code in (200, 202):
                        ok_n += len(part)
                        break
                    if res.status_code >= 500 or res.status_code == 429:
                        await asyncio.sleep(0.6 * (attempt + 1))
                        continue
                    logger.warning(
                        "usage-report HTTP %s panel=%s",
                        res.status_code,
                        pid,
                    )
                    break
                except Exception:  # noqa: BLE001
                    await asyncio.sleep(0.6 * (attempt + 1))
            await asyncio.sleep(0.25)
    return ok_n


async def _targeted_usage_map(
    adapter: Any,
    names: list[str],
    *,
    concurrency: int = 10,
    panel_id: int = 0,
    deadline: float | None = None,
    prefer_bulk: bool | None = None,
) -> dict[str, int]:
    """Warm/Hot path — bulk list when >5 names; else exact get_usage only.

    Never runs unbounded sequential per-user loops that block a Sem slot for
    minutes (was panel=59 warm listed=66 dt=309s).
    """
    out: dict[str, int] = {}
    if not names:
        return out
    pid = int(panel_id or 0)
    want = [n for n in names if n and not (pid and _usage_404_hit(pid, n))]
    if not want:
        return out
    use_bulk = (
        bool(prefer_bulk)
        if prefer_bulk is not None
        else len(want) > int(_BULK_NAME_THRESHOLD)
    )
    if use_bulk:
        bulk, _, _ = await _list_usage_map(
            adapter, start_offset=0, deadline=deadline
        )
        if bulk:
            want_l = {n.lower(): n for n in want}
            for raw_name, used in bulk.items():
                key = str(raw_name).strip().lower()
                if key in want_l:
                    out[want_l[key]] = max(0, int(used or 0))
            if out:
                return out
        # Bulk empty / unsupported — fall through to tiny exact fan-out.
        want = want[: max(1, int(_BULK_NAME_THRESHOLD))]

    sem = asyncio.Semaphore(max(1, min(8, int(concurrency))))

    async def _one(uname: str) -> None:
        if deadline is not None and time.monotonic() >= float(deadline):
            return
        async with sem:
            r = await _get_usage_exact(
                adapter, uname, panel_id=pid, timeout=2.0
            )
            if r is None:
                return
            out[uname] = max(0, int(getattr(r, "used_traffic", 0) or 0))

    try:
        remain = None
        if deadline is not None:
            remain = max(0.2, float(deadline) - time.monotonic())
        await asyncio.wait_for(
            asyncio.gather(*[_one(n) for n in want], return_exceptions=True),
            timeout=remain,
        )
    except asyncio.TimeoutError:
        pass
    return out


def _need_cold_sweep(pid: int, wave_n: int = 0) -> bool:
    """Per-panel Cold due: 60s since last complete sweep, or mid-checkpoint.

    Do NOT force every panel Cold on the same wave index — that stampeded
    all 20 panels into 90s+ waves and starved Warm forever.
    """
    _ = wave_n
    if int(_COLD_OFFSET.get(pid, 0) or 0) > 0:
        return True
    last = float(_LAST_COLD_TS.get(pid, 0.0) or 0.0)
    return (time.monotonic() - last) >= float(_COLD_EVERY_SEC)


async def _collect_one(
    panel: dict[str, Any],
    *,
    wave_n: int,
    force_mode: str | None = None,
) -> None:
    """Warm targeted poll or Cold full-page sweep; delta-only report.

    Cold wall is 60s with offset checkpoint so large panels finish across
    waves instead of restarting from 0 after a mid-sweep timeout.
    """
    if not MASTER_URL or not WORKER_TOKEN:
        return
    pid = int(panel.get("id") or 0)
    if not pid:
        return
    if _circuit_open(pid):
        logger.info("collect skip panel=%s circuit-open", pid)
        return

    if force_mode == "warm":
        cold = False
    elif force_mode == "cold":
        cold = True
    else:
        cold = _need_cold_sweep(pid, wave_n) or (pid not in _LAST_MAP)
    wall = _PANEL_COLD_TIMEOUT if cold else _PANEL_COLLECT_TIMEOUT
    t0 = time.monotonic()
    deadline = t0 + float(wall) - 0.5
    try:
        # Hard outer wall — Cold 60s / Warm 15s; checkpoint before cancel.
        return await asyncio.wait_for(
            _collect_one_inner(
                panel,
                pid=pid,
                cold=cold,
                wave_n=wave_n,
                wall=wall,
                t0=t0,
                deadline=deadline,
            ),
            timeout=float(wall) + 1.0,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "collect hard-timeout panel=%s wall=%.0fs mode=%s resume_offset=%s",
            pid,
            wall,
            "cold" if cold else "warm",
            int(_COLD_OFFSET.get(pid, 0) or 0),
        )
        # Live fetch stalled — still push cache so master last_sync_at moves.
        try:
            n = await _post_cache_sync_touch(
                pid, mode="cold" if cold else "warm"
            )
            if n:
                logger.info(
                    "cache sync-touch panel=%s mode=%s sent=%s after-timeout",
                    pid,
                    "cold" if cold else "warm",
                    n,
                )
        except Exception:  # noqa: BLE001
            logger.debug("cache sync-touch failed panel=%s", pid, exc_info=True)
        return


async def _collect_one_inner(
    panel: dict[str, Any],
    *,
    pid: int,
    cold: bool,
    wave_n: int,
    wall: float,
    t0: float,
    deadline: float,
) -> None:
    adapter = None
    cold_complete = False
    try:
        adapter = _build_adapter(panel)
        if cold:
            start_off = int(_COLD_OFFSET.get(pid, 0) or 0)
            usage_map, next_off, complete = await _list_usage_map(
                adapter,
                start_offset=start_off,
                deadline=deadline,
            )
            if complete:
                _COLD_OFFSET.pop(pid, None)
                _LAST_COLD_TS[pid] = time.monotonic()
                cold_complete = True
            else:
                _COLD_OFFSET[pid] = int(next_off or start_off)
                logger.info(
                    "collect checkpoint panel=%s offset=%s listed=%s",
                    pid,
                    _COLD_OFFSET[pid],
                    len(usage_map),
                )
            mode = "cold"
            # Any completed cold wave (incl. checkpoint finish) rotates
            # unchanged meters so master can refresh last_sync_at.
            replace_map = bool(complete)
        else:
            tier = _TIERS.get(pid) or {}
            names = list(
                dict.fromkeys(
                    list(tier.get("hot") or []) + list(tier.get("warm") or [])
                )
            )
            prev = _LAST_MAP.get(pid) or {}
            for name, used in prev.items():
                if int(used or 0) > 0 and name not in names:
                    if len(names) < 800:
                        names.append(name)
            names = [n for n in names if not _usage_404_hit(pid, n)]
            # Bulk /api/users when >5 targets; hard 15s wall — never 300s slots.
            usage_map = await _targeted_usage_map(
                adapter,
                names[:800],
                panel_id=pid,
                deadline=deadline,
                prefer_bulk=len(names) > int(_BULK_NAME_THRESHOLD),
            )
            mode = "warm"
            replace_map = False
        if not usage_map:
            if cold and cold_complete:
                logger.info(
                    "cold_sweep_complete panel=%s listed=0 changed=0 dt=%.2fs",
                    pid,
                    time.monotonic() - t0,
                )
            elif cold and int(_COLD_OFFSET.get(pid, 0) or 0) == 0:
                _circuit_fail(pid)
            return
        _circuit_ok(pid)
        meters = _delta_meters(
            pid,
            usage_map,
            force_full=bool(cold and cold_complete),
            replace_map=replace_map,
        )
        listed = len(usage_map)
        changed = len(meters)
        if cold and cold_complete:
            # Always log full-fleet coverage — even when delta is empty.
            logger.info(
                "cold_sweep_complete panel=%s listed=%s changed=%s dt=%.2fs",
                pid,
                listed,
                changed,
                time.monotonic() - t0,
            )
        if not meters:
            if cold and usage_map:
                # Still prove the panel was scanned — master stamps last_sync_at.
                wave = int(time.time() // 60)
                meters = [
                    {"remote_username": str(n), "used_traffic": int(u or 0)}
                    for n, u in usage_map.items()
                    if str(n).strip() and (hash(str(n).strip().lower()) + wave) % 3 == 0
                ][:4000]
            if not meters:
                if not cold:
                    logger.info(
                        "report skip panel=%s mode=%s no-delta listed=%s",
                        pid,
                        mode,
                        listed,
                    )
                return
        sent = await _post_meter_chunks(
            pid=pid,
            meters=meters,
            listed=listed,
            dt_ms=int((time.monotonic() - t0) * 1000),
            full_sync=bool(cold and cold_complete),
            mode=mode,
        )
        logger.info(
            "reported panel=%s mode=%s sent=%s listed=%s full=0 dt=%.1fs",
            pid,
            mode,
            sent,
            listed,
            time.monotonic() - t0,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "collect timeout panel=%s wall=%.0fs resume_offset=%s",
            pid,
            wall,
            int(_COLD_OFFSET.get(pid, 0) or 0),
        )
    except Exception:  # noqa: BLE001
        _circuit_fail(pid)
        raise
    finally:
        if adapter is not None:
            close = getattr(adapter, "close", None)
            if callable(close):
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    pass


_WAVE_N = 0


async def _collect_loop() -> None:
    """Independent warm/cold wave loop — never shares locks with priority-probe.

    Warm every ``_INTERVAL`` (20s); Cold full-panel sweep every ``_COLD_EVERY_SEC``
    (60s). Dedicated ``Semaphore(4)`` for panels + separate report gate so
    priority-probe Sem(12) / hot posts cannot stall or reset this loop.
    """
    global _WAVE_N, _COLLECT_PANEL_SEM, _COLD_PANEL_SEM
    await asyncio.sleep(3)
    if _COLLECT_PANEL_SEM is None:
        _COLLECT_PANEL_SEM = asyncio.Semaphore(max(1, int(_COLLECT_CONC)))
    if _COLD_PANEL_SEM is None:
        _COLD_PANEL_SEM = asyncio.Semaphore(max(1, int(_COLD_CONC)))
    while True:
        t0 = time.monotonic()
        try:
            # Yield collect slots while Master is mid-create (large multi-panel).
            if _CREATE_INFLIGHT >= 8:
                await asyncio.sleep(1.5)
            try:
                await _heartbeat_once()
            except Exception:  # noqa: BLE001
                logger.debug("collect heartbeat failed", exc_info=True)
            panels = list(_ASSIGNED)
            if panels:
                _WAVE_N += 1
                wave_n = _WAVE_N
                warm_sem = _COLLECT_PANEL_SEM
                cold_sem = _COLD_PANEL_SEM
                cold_n = warm_n = 0
                # Stagger Cold: at most cold Sem slots per wave; rest stay Warm
                # so the 20s cadence never dies when many panels share a 60s due.
                cold_due: list[dict[str, Any]] = []
                warm_due: list[dict[str, Any]] = []
                for p in panels:
                    pid = int(p.get("id") or 0)
                    if not pid:
                        continue
                    if _need_cold_sweep(pid, wave_n) or pid not in _LAST_MAP:
                        cold_due.append(p)
                    else:
                        warm_due.append(p)
                # Resume mid-sweep first, then heavy/large panels, then the rest.
                _HEAVY_COLD = {3, 4, 5, 17, 25, 39, 60, 62, 63}
                cold_due.sort(
                    key=lambda p: (
                        0
                        if int(_COLD_OFFSET.get(int(p.get("id") or 0), 0) or 0) > 0
                        else 1,
                        0 if int(p.get("id") or 0) in _HEAVY_COLD else 1,
                        int(p.get("id") or 0),
                    )
                )
                # Prefer cold when due — warm timeouts were starving 60s sweeps.
                cold_cap = max(2, int(_COLD_CONC) * 2)
                cold_batch = cold_due[:cold_cap]
                # Cap warm fan-out so cold always gets slots in the same wave.
                warm_cap = max(2, int(_COLLECT_CONC))
                warm_batch = (warm_due + cold_due[cold_cap:])[:warm_cap]

                async def _run(p: dict[str, Any], mode: str) -> None:
                    nonlocal cold_n, warm_n
                    sem = cold_sem if mode == "cold" else warm_sem
                    async with sem:
                        try:
                            await _collect_one(
                                p, wave_n=wave_n, force_mode=mode
                            )
                            if mode == "cold":
                                cold_n += 1
                            else:
                                warm_n += 1
                        except Exception:  # noqa: BLE001
                            # Never let one panel abort the wave / future waves.
                            logger.warning(
                                "collect failed panel=%s",
                                p.get("id"),
                                exc_info=True,
                            )

                # Warm is the wave barrier (20s cadence). Cold is background so
                # a 90s panel list never stalls the next warm tick. Never wait
                # forever for slow warm panels — cap the barrier at interval+5s
                # and let stragglers finish under warm Sem without blocking n+1.
                warm_tasks = [
                    asyncio.create_task(_run(p, "warm")) for p in warm_batch
                ]
                for p in cold_batch:
                    asyncio.create_task(
                        _run(p, "cold"),
                        name=f"cold-panel-{p.get('id')}",
                    )
                pending_warm = 0
                if warm_tasks:
                    done, pending = await asyncio.wait(
                        warm_tasks,
                        timeout=max(15.0, float(_INTERVAL) + 5.0),
                    )
                    pending_warm = len(pending)
                    for t in done:
                        exc = t.exception() if not t.cancelled() else None
                        if isinstance(exc, Exception):
                            logger.warning(
                                "warm task error: %s", exc, exc_info=exc
                            )
                logger.info(
                    "collect wave panels=%s warm=%s warm_pending=%s cold_spawned=%s dt=%.1fs interval=%s n=%s",
                    len(panels),
                    warm_n,
                    pending_warm,
                    len(cold_batch),
                    time.monotonic() - t0,
                    _INTERVAL,
                    wave_n,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("collect loop error")
        elapsed = time.monotonic() - t0
        # Fixed warm cadence — long cold panels must not reset the 25s timer
        # forever; sleep the remainder of 25s (min 1s) so the next wave starts.
        await asyncio.sleep(max(1.0, float(_INTERVAL) - elapsed))


async def _priority_loop() -> None:
    """Hot-tier poll every ~3s on its own HTTP pool — never blocked by collect/KILL."""
    await asyncio.sleep(2)
    while True:
        t0 = time.monotonic()
        panels_n = 0
        users_n = 0
        sent_n = 0
        try:
            if not MASTER_URL or not WORKER_TOKEN:
                await asyncio.sleep(3.0)
                continue
            if not _ASSIGNED:
                await asyncio.sleep(3.0)
                continue
            client = await _http_priority()
            items: list[dict[str, Any]] = []
            try:
                res = await client.get(
                    f"{MASTER_URL}/api/internal/worker/priority-probe",
                    headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
                    params={"limit": _PRIO_FETCH_LIMIT},
                )
                if res.status_code == 200:
                    data = res.json() if res.content else {}
                    raw = (
                        (data or {}).get("items")
                        or (data or {}).get("probes")
                        or (data or {}).get("remotes")
                        or []
                    )
                    items = list(raw or [])
                elif res.status_code == 404:
                    logger.warning("priority-probe endpoint missing on Master")
                elif res.status_code == 401:
                    logger.warning("priority-probe auth failed HTTP 401")
                else:
                    logger.debug("priority-probe HTTP %s", res.status_code)
            except Exception:  # noqa: BLE001
                logger.debug("priority-probe fetch failed", exc_info=True)

            # Always top up from local Hot tiers so ticks stay dense (200–500).
            if len(items) < 200:
                for pid, name in _hot_tier_slice(max(200, 500 - len(items))):
                    items.append({"panel_id": pid, "remote_username": name})

            by_panel: dict[int, list[str]] = {}
            seen: set[tuple[int, str]] = set()
            for it in items:
                if not isinstance(it, dict):
                    continue
                try:
                    pid = int(
                        it.get("panel_id")
                        or it.get("panelId")
                        or it.get("pid")
                        or 0
                    )
                    name = str(
                        it.get("remote_username")
                        or it.get("username")
                        or it.get("remote")
                        or it.get("name")
                        or ""
                    ).strip()
                except Exception:  # noqa: BLE001
                    continue
                if not pid or not name:
                    continue
                key = (pid, name.lower())
                if key in seen:
                    continue
                seen.add(key)
                if _usage_404_hit(pid, name):
                    continue
                by_panel.setdefault(pid, []).append(name)

            panels_by_id = {
                int(p.get("id") or 0): p for p in _ASSIGNED if p.get("id")
            }
            # All panels in parallel (Sem 12) — 200–500 users/tick target.
            panel_items = [
                (pid, names)
                for pid, names in by_panel.items()
                if panels_by_id.get(pid) and not _circuit_open(pid)
            ]
            users_n = sum(len(n) for _, n in panel_items)
            panel_sem = asyncio.Semaphore(_PRIO_PANEL_SEM)
            sent_lock = asyncio.Lock()

            async def _probe_panel(pid: int, names: list[str]) -> None:
                nonlocal panels_n, sent_n
                panel = panels_by_id.get(pid)
                if not panel:
                    return
                async with panel_sem:
                    adapter = None
                    try:
                        adapter = _build_adapter(panel)
                        batch = names[:80]
                        # Hot is ≤15 names/panel — exact parallel get_usage with
                        # a 5s wall. Never bulk-list 30k users here (that made
                        # ticks 34s+). Warm still uses bulk when >5 targets.
                        prio_deadline = time.monotonic() + float(
                            _PANEL_PRIO_TIMEOUT
                        )
                        usage = await _targeted_usage_map(
                            adapter,
                            batch,
                            panel_id=pid,
                            deadline=prio_deadline,
                            prefer_bulk=False,
                            concurrency=16,
                        )
                        panels_n += 1
                        # ALWAYS post every probed meter (not delta-only).
                        # Delta-only left Master stale when LAST_MAP advanced
                        # but the usage-report POST was dropped/deferred.
                        if usage:
                            norm = _canonicalize_meter_map(
                                {
                                    str(n).strip().lower(): int(u or 0)
                                    for n, u in usage.items()
                                    if str(n).strip()
                                }
                            )
                            meters = [
                                {
                                    "remote_username": name,
                                    "used_traffic": int(used or 0),
                                }
                                for name, used in norm.items()
                            ]
                            # Keep local cache in sync only after a successful post
                            # so a failed report can still retry next tick.
                            n_sent = await _post_meter_chunks(
                                pid=pid,
                                meters=meters,
                                listed=len(meters),
                                dt_ms=int((time.monotonic() - t0) * 1000),
                                full_sync=False,
                                mode="hot",
                            )
                            if n_sent:
                                prev = dict(_LAST_MAP.get(pid) or {})
                                prev.update(norm)
                                _LAST_MAP[pid] = prev
                                _LAST_MAP_TS[pid] = time.monotonic()
                                _persist_meters(pid, dict(norm), replace=False)
                            async with sent_lock:
                                sent_n += int(n_sent or 0)
                            logger.info(
                                "reported panel=%s mode=hot sent=%s listed=%s full=0 dt=%.2fs",
                                pid,
                                n_sent,
                                len(meters),
                                time.monotonic() - t0,
                            )
                    except Exception:  # noqa: BLE001
                        _circuit_fail(pid)
                        logger.debug(
                            "priority-probe failed panel=%s", pid, exc_info=True
                        )
                    finally:
                        if adapter is not None:
                            close = getattr(adapter, "close", None)
                            if callable(close):
                                try:
                                    await close()
                                except Exception:  # noqa: BLE001
                                    pass

            if panel_items:
                # Sem(24) + 2.4s/panel + 2.8s tick wall → all panels in <3s.
                # Do NOT await cancelled stragglers — that re-introduced 5s+ ticks.
                tasks = [
                    asyncio.create_task(_probe_panel(pid, names))
                    for pid, names in panel_items
                ]
                _done, pending = await asyncio.wait(
                    tasks, timeout=float(_PRIO_TICK_TIMEOUT)
                )
                for t in pending:
                    t.cancel()

            logger.info(
                "priority-probe panels=%s/%s users=%s sent=%s dt=%.2fs",
                panels_n,
                len(panel_items),
                users_n,
                sent_n,
                time.monotonic() - t0,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("priority loop error", exc_info=True)
        elapsed = time.monotonic() - t0
        await asyncio.sleep(max(0.5, 3.0 - elapsed))


async def _assignment_loop() -> None:
    """Pull exact leased panels + tiers from Master every ~20s (failover sync)."""
    global _ASSIGNED, _INTERVAL, _COLD_EVERY_SEC, _TIERS
    await asyncio.sleep(2)
    while True:
        try:
            if not MASTER_URL or not WORKER_TOKEN:
                await asyncio.sleep(10)
                continue
            client = await _http_cmds()
            res = await client.get(
                f"{MASTER_URL}/api/internal/worker/assignment",
                headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
            )
            if res.status_code == 404:
                await asyncio.sleep(30)
                continue
            if res.status_code == 200:
                data = res.json() if res.content else {}
                panels = list((data or {}).get("panels") or [])
                _ASSIGNED = panels
                try:
                    _INTERVAL = max(
                        15, min(300, int((data or {}).get("interval_sec") or 20))
                    )
                    _COLD_EVERY_SEC = float(
                        (data or {}).get("cold_every_sec") or 60
                    )
                except (TypeError, ValueError):
                    pass
                raw_tiers = (data or {}).get("tiers") or {}
                parsed: dict[int, dict[str, list[str]]] = {}
                for k, v in raw_tiers.items():
                    try:
                        pid = int(k)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(v, dict):
                        continue
                    parsed[pid] = {
                        "hot": [str(x) for x in (v.get("hot") or [])],
                        "warm": [str(x) for x in (v.get("warm") or [])],
                    }
                _TIERS = parsed
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("assignment loop error", exc_info=True)
        await asyncio.sleep(20.0)


def _kill_panel_sem(pid: int) -> asyncio.Semaphore:
    """≤3 concurrent disable PUTs per remote panel."""
    key = int(pid or 0)
    sem = _KILL_PANEL_SEMS.get(key)
    if sem is None:
        sem = asyncio.Semaphore(3)
        _KILL_PANEL_SEMS[key] = sem
    return sem


def _kill_backoff_delay(attempt: int) -> float:
    """Jittered 3s → 10s → 25s before retry attempts 1/2/3."""
    import random

    idx = max(0, min(int(attempt) - 1, len(_KILL_BACKOFF_SEC) - 1))
    base = float(_KILL_BACKOFF_SEC[idx])
    return base * (0.85 + 0.3 * random.random())


def _kill_acc_key(acc: dict[str, Any]) -> tuple[int, str] | None:
    panel = acc.get("panel") if isinstance(acc, dict) else None
    if not isinstance(panel, dict):
        return None
    pid = int(panel.get("id") or 0)
    uname = str(acc.get("remote_username") or "").strip().lower()
    if not pid or not uname:
        return None
    return (pid, uname)


def _park_kill_account(acc: dict[str, Any]) -> None:
    key = _kill_acc_key(acc)
    if key is None:
        return
    _KILL_PARKED[key] = acc


def _unpark_kill_account(acc: dict[str, Any]) -> None:
    key = _kill_acc_key(acc)
    if key is not None:
        _KILL_PARKED.pop(key, None)


async def _disable_one_account(acc: dict[str, Any], *, timeout: float = 4.0) -> bool:
    """Hard-cut one remote: status=disabled AND data_limit <= current used.

    ``data_limit=0`` means unlimited on most panels and must never be used for
    quota kill — active Xray sessions keep downloading until the remote cap
    is clamped to the bytes already used.

    HTTP 404 is terminal: mark negative cache and return True (no kill_retry).
    """
    panel = acc.get("panel")
    if not isinstance(panel, dict):
        return False
    uname = str(acc.get("remote_username") or "").strip()
    if not uname:
        return False
    pid = int(panel.get("id") or 0)
    if pid and _usage_404_hit(pid, uname):
        _unpark_kill_account(acc)
        return True  # already known missing — done, never retry
    if pid and _kill_panel_blocked(pid):
        return False  # panel 5xx storm — caller parks/retries after cooldown
    used = max(0, int(acc.get("used_traffic") or 0))
    adapter = None
    async with _kill_panel_sem(pid):
        try:
            adapter = _build_adapter(panel)
            if used <= 0:
                # Best-effort live read so we can clamp the remote cap.
                try:
                    try:
                        r = await asyncio.wait_for(
                            adapter.get_usage(uname, None, expand_links=False),
                            timeout=min(3.0, timeout),
                        )
                    except TypeError:
                        r = await asyncio.wait_for(
                            adapter.get_usage(uname, None),
                            timeout=min(3.0, timeout),
                        )
                    if _result_is_404(r):
                        if pid:
                            _usage_404_mark(pid, uname)
                        _unpark_kill_account(acc)
                        return True
                    used = max(0, int(getattr(r, "used_traffic", 0) or 0))
                except Exception as exc:  # noqa: BLE001
                    if _is_404_err(exc):
                        if pid:
                            _usage_404_mark(pid, uname)
                        _unpark_kill_account(acc)
                        return True
                    used = 0
            hard_limit = max(1, used) if used > 0 else 1
            cfg: dict[str, Any] = {"_freeze_only": True}
            rid = str(acc.get("remote_id") or "").strip()
            if rid and not rid.startswith("{"):
                cfg["_remote_id"] = rid
            payload = _payload_from(
                {
                    "username": uname,
                    "status": "disabled",
                    "data_limit": hard_limit,
                    "expire": 0,
                    "config": cfg,
                }
            )
            res = await asyncio.wait_for(adapter.modify_user(payload), timeout=timeout)
            if bool(getattr(res, "success", False)):
                if pid:
                    _kill_note_ok(pid)
                _unpark_kill_account(acc)
                return True
            # Already gone on remote — treat as done (no kill_retry storm).
            if _result_is_404(res) or _is_404_err(getattr(res, "error", None)):
                if pid:
                    _usage_404_mark(pid, uname)
                _unpark_kill_account(acc)
                return True
            if _is_5xx_err(getattr(res, "error", None)) or _is_5xx_err(res):
                if pid:
                    _kill_note_5xx(pid)
            return False
        except Exception as exc:  # noqa: BLE001
            if _is_404_err(exc):
                if pid:
                    _usage_404_mark(pid, uname)
                _unpark_kill_account(acc)
                return True
            if _is_5xx_err(exc):
                if pid:
                    _kill_note_5xx(pid)
            return False
        finally:
            if adapter is not None:
                close = getattr(adapter, "close", None)
                if callable(close):
                    try:
                        await close()
                    except Exception:  # noqa: BLE001
                        pass


async def _handle_kill_user(cmd: dict[str, Any]) -> None:
    accounts = list(cmd.get("accounts") or [])
    if not accounts:
        return
    uid = cmd.get("user_id")
    t0 = time.monotonic()
    kill_log = logging.getLogger("astra.worker.kill")
    # Skip known-404; cooldown panels still retry later (not counted as ok).
    runnable = []
    skipped_404 = 0
    for acc in accounts:
        panel = acc.get("panel") if isinstance(acc, dict) else None
        pid = int((panel or {}).get("id") or 0) if isinstance(panel, dict) else 0
        uname = str((acc or {}).get("remote_username") or "")
        if pid and _usage_404_hit(pid, uname):
            skipped_404 += 1
            continue
        runnable.append(acc)
    results = await asyncio.gather(
        *[_disable_one_account(a, timeout=5.0) for a in runnable],
        return_exceptions=True,
    )
    ok_n = skipped_404
    fail_n = 0
    for acc, res in zip(runnable, results):
        if res is True:
            ok_n += 1
            continue
        fail_n += 1
        # First retry after ~3s; up to 3 attempts total. Never for 404.
        _KILL_RETRY.append((time.monotonic() + _kill_backoff_delay(1), 1, acc))
    kill_log.info(
        "KILL_USER executed user_id=%s ok=%s failed=%s skipped_404=%s accounts=%s dt=%.2fs",
        uid,
        ok_n,
        fail_n,
        skipped_404,
        len(accounts),
        time.monotonic() - t0,
    )


async def _kill_retry_tick() -> None:
    if not _KILL_RETRY:
        return
    now = time.monotonic()
    due: list[tuple[int, dict[str, Any]]] = []
    rest: list[tuple[float, int, dict[str, Any]]] = []
    for due_ts, attempt, acc in _KILL_RETRY:
        if due_ts <= now:
            due.append((attempt, acc))
        else:
            rest.append((due_ts, attempt, acc))
    _KILL_RETRY[:] = rest
    for attempt, acc in due:
        panel = acc.get("panel") if isinstance(acc, dict) else None
        pid = int((panel or {}).get("id") or 0) if isinstance(panel, dict) else 0
        uname = str((acc or {}).get("remote_username") or "")
        if pid and _usage_404_hit(pid, uname):
            _unpark_kill_account(acc)
            continue  # never retry 404
        if pid and _kill_panel_blocked(pid):
            # Soft defer until cooldown ends — do not burn an attempt.
            _KILL_RETRY.append((time.monotonic() + 15.0, attempt, acc))
            continue
        ok = await _disable_one_account(acc, timeout=5.0)
        if ok:
            continue
        if attempt >= int(_KILL_MAX_ATTEMPTS):
            logger.warning(
                "kill_retry park panel=%s user=%s (max %s attempts)",
                pid,
                uname,
                _KILL_MAX_ATTEMPTS,
            )
            _park_kill_account(acc)
            continue
        nxt = int(attempt) + 1
        _KILL_RETRY.append(
            (time.monotonic() + _kill_backoff_delay(nxt), nxt, acc)
        )


async def _kill_parked_resync() -> None:
    """Every 5 minutes: re-dispatch parked kills still not confirmed disabled."""
    global _LAST_KILL_RESYNC
    now = time.monotonic()
    if now - float(_LAST_KILL_RESYNC or 0.0) < float(_KILL_RESYNC_EVERY):
        return
    _LAST_KILL_RESYNC = now
    parked = list(_KILL_PARKED.values())
    if not parked:
        return
    logger.info("kill_parked_resync n=%s", len(parked))
    for acc in parked:
        panel = acc.get("panel") if isinstance(acc, dict) else None
        pid = int((panel or {}).get("id") or 0) if isinstance(panel, dict) else 0
        uname = str((acc or {}).get("remote_username") or "")
        if pid and _usage_404_hit(pid, uname):
            _unpark_kill_account(acc)
            continue
        if pid and _kill_panel_blocked(pid):
            continue
        ok = await _disable_one_account(acc, timeout=5.0)
        if ok:
            continue
        # Fresh retry cycle for stubborn remotes.
        _KILL_RETRY.append((time.monotonic() + _kill_backoff_delay(1), 1, acc))


async def _commands_loop() -> None:
    """Long-poll Master for KILL_USER; wakes in <100ms when enqueued."""
    await asyncio.sleep(2)
    while True:
        try:
            if not MASTER_URL or not WORKER_TOKEN:
                await asyncio.sleep(5)
                continue
            client = await _http_cmds()
            try:
                res = await client.get(
                    f"{MASTER_URL}/api/internal/worker/commands",
                    headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
                    params={"timeout": 15, "limit": 20},
                    timeout=httpx.Timeout(20.0, connect=5.0),
                )
            except Exception:  # noqa: BLE001
                await _kill_retry_tick()
                await _kill_parked_resync()
                await asyncio.sleep(2)
                continue
            if res.status_code == 404:
                # Older master — fall back to short sleep heartbeat via priority.
                await _kill_retry_tick()
                await _kill_parked_resync()
                await asyncio.sleep(10)
                continue
            if res.status_code != 200:
                await _kill_retry_tick()
                await _kill_parked_resync()
                await asyncio.sleep(2)
                continue
            data = res.json() if res.content else {}
            cmds = list((data or {}).get("commands") or [])
            for cmd in cmds:
                if str(cmd.get("op") or "") == "KILL_USER":
                    try:
                        await _handle_kill_user(cmd)
                    except Exception:  # noqa: BLE001
                        logger.exception("KILL_USER failed")
            await _kill_retry_tick()
            await _kill_parked_resync()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("commands loop error", exc_info=True)
            await asyncio.sleep(2)


@app.on_event("startup")
async def _startup() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info(
        "worker start name=%s master=%s ver=%s",
        WORKER_NAME,
        MASTER_URL or "(none)",
        WORKER_VERSION,
    )
    try:
        removed = await asyncio.to_thread(_cleanup_meter_cache_duplicates)
        if removed:
            logger.info("meter_cache cleaned numeric twins removed=%s", removed)
    except Exception:  # noqa: BLE001
        logger.debug("meter_cache cleanup skipped", exc_info=True)
    _ensure_bg_tasks()
