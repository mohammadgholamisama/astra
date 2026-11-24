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
WORKER_VERSION = "1.0.3"

# Assigned panels for background usage collection
_ASSIGNED: list[dict[str, Any]] = []
_INTERVAL = 120  # full usage sweep every 2 minutes
_COLLECT_CONC = 1  # one panel at a time — protects master CPU
_REPORT_CONC = 1  # serialize posts to master
_METER_CHUNK = 2500  # fewer HTTP round-trips; master coalesces anyway
_LAST_MAP: dict[int, dict[str, int]] = {}  # panel_id → last sent meters
_COLLECT_TASK: asyncio.Task | None = None
_REPORT_SEM: asyncio.Semaphore | None = None

app = FastAPI(title="Astra Worker", version=WORKER_VERSION)


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
    adapter = _build_adapter(body.panel)
    op = (body.op or "").strip()
    args = body.args or {}
    try:
        if op == "test_connection":
            res = await adapter.test_connection()
        elif op == "create_user":
            res = await adapter.create_user(_payload_from(args))
        elif op == "modify_user":
            res = await adapter.modify_user(_payload_from(args))
        elif op == "delete_user":
            res = await adapter.delete_user(
                str(args.get("username") or ""), args.get("remote_id")
            )
        elif op == "get_usage":
            uname = str(args.get("username") or "")
            rid = args.get("remote_id")
            expand = bool(args.get("expand_links", True))
            try:
                res = await adapter.get_usage(uname, rid, expand_links=expand)
            except TypeError:
                res = await adapter.get_usage(uname, rid)
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
    global _ASSIGNED, _INTERVAL, MASTER_URL, _COLLECT_TASK
    if body.master_url:
        MASTER_URL = str(body.master_url).rstrip("/")
        os.environ["ASTRA_MASTER_URL"] = MASTER_URL
    _INTERVAL = max(60, min(300, int(body.interval_sec or 120)))
    _ASSIGNED = list(body.panels or [])
    if _COLLECT_TASK is None or _COLLECT_TASK.done():
        _COLLECT_TASK = asyncio.create_task(_collect_loop(), name="usage-collect")
    return {"ok": True, "panels": len(_ASSIGNED), "interval": _INTERVAL}


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
        async with httpx.AsyncClient(timeout=8.0, verify=False) as client:
            await client.post(
                f"{MASTER_URL}/api/internal/worker/heartbeat",
                headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
                json={
                    "version": WORKER_VERSION,
                    "panels": [int(p.get("id") or 0) for p in _ASSIGNED if p.get("id")],
                },
            )
    except Exception:  # noqa: BLE001
        logger.debug("heartbeat failed", exc_info=True)


def _used_bytes(data: dict[str, Any]) -> int:
    try:
        v = data.get("used_traffic")
        if v is not None and int(v) > 0:
            return int(v)
        for key in ("lifetime_used_traffic", "total_traffic", "current_usage"):
            v = data.get(key)
            if v is not None and int(v) > 0:
                return int(v)
        if v is not None:
            return max(0, int(v))
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


async def _list_usage_map(adapter: Any, *, max_pages: int = 80) -> dict[str, int]:
    """Bulk usage map: Marzban-family paginated list, or Sanaei inbounds."""
    from app.adapters.marzban import MarzbanAdapter
    from app.adapters.sanaei import SanaeiAdapter

    if isinstance(adapter, SanaeiAdapter):
        return await _sanaei_usage_map(adapter)
    if not isinstance(adapter, MarzbanAdapter):
        return {}
    out: dict[str, int] = {}
    client, headers = await adapter._authed()
    timeout = httpx.Timeout(25.0, connect=8.0)
    offset = 0
    page_size = 200
    for _ in range(max(1, int(max_pages))):
        resp = await client.get(
            "/api/users",
            headers=headers,
            params={"limit": page_size, "offset": offset},
            timeout=timeout,
        )
        if resp.status_code >= 400:
            break
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            break
        users = []
        if isinstance(payload, dict):
            users = payload.get("users") or payload.get("items") or []
        elif isinstance(payload, list):
            users = payload
        if not isinstance(users, list) or not users:
            break
        for u in users:
            if not isinstance(u, dict):
                continue
            used = _used_bytes(u)
            name = str(u.get("username") or "").strip()
            if name:
                out[name] = max(int(out.get(name) or 0), used)
            # PasarGuard remotes often keyed by numeric id
            uid = u.get("id")
            if uid is not None and str(uid).strip():
                kid = str(uid).strip()
                out[kid] = max(int(out.get(kid) or 0), used)
        if len(users) < page_size:
            break
        offset += page_size
    return out


def _delta_meters(
    pid: int, usage_map: dict[str, int], *, force_full: bool = False
) -> list[dict[str, int | str]]:
    """Only send changed meters after the first full snapshot (cuts 32k→small).

    Full snapshot sends used>0 only (zeros don't change Astra meters and
    were burning master CPU on 20×1500 empty-ish applies).
    """
    prev = _LAST_MAP.get(pid)
    if force_full or prev is None:
        items = [(n, u) for n, u in usage_map.items() if int(u or 0) > 0]
    else:
        items = [
            (name, used)
            for name, used in usage_map.items()
            if int(prev.get(name, -1)) != int(used)
        ]
    _LAST_MAP[pid] = dict(usage_map)
    return [
        {"remote_username": str(name), "used_traffic": int(used or 0)}
        for name, used in items
    ]


async def _post_meter_chunks(
    *,
    pid: int,
    meters: list[dict[str, int | str]],
    listed: int,
    dt_ms: int,
) -> int:
    """POST meters to master in chunks; one in-flight report at a time."""
    global _REPORT_SEM
    if _REPORT_SEM is None:
        _REPORT_SEM = asyncio.Semaphore(max(1, int(_REPORT_CONC)))
    if not meters:
        return 0
    ok_n = 0
    chunk = max(200, int(_METER_CHUNK))
    async with _REPORT_SEM:
        async with httpx.AsyncClient(timeout=60.0, verify=False) as client:
            for i in range(0, len(meters), chunk):
                part = meters[i : i + chunk]
                for attempt in range(3):
                    try:
                        res = await client.post(
                            f"{MASTER_URL}/api/internal/worker/usage-report",
                            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
                            json={
                                "panel_id": pid,
                                "meters": part,
                                "listed": listed,
                                "dt_ms": dt_ms,
                            },
                        )
                        if res.status_code in (200, 202):
                            ok_n += len(part)
                            break
                        if res.status_code >= 500 or res.status_code == 429:
                            await asyncio.sleep(0.8 * (attempt + 1))
                            continue
                        logger.warning(
                            "usage-report HTTP %s panel=%s",
                            res.status_code,
                            pid,
                        )
                        break
                    except Exception:  # noqa: BLE001
                        await asyncio.sleep(0.8 * (attempt + 1))
                # Pace chunks — master coalesces; don't stampede apply lock.
                await asyncio.sleep(0.4)
    return ok_n


async def _collect_one(panel: dict[str, Any], *, wave_n: int) -> None:
    """Pull usage map from remote panel and report deltas to master."""
    if not MASTER_URL or not WORKER_TOKEN:
        return
    pid = int(panel.get("id") or 0)
    if not pid:
        return
    adapter = _build_adapter(panel)
    t0 = time.monotonic()
    try:
        usage_map = await _list_usage_map(adapter)
        if not usage_map:
            return
        # Full resync rarely (every ~16 waves ≈ 30+ min) — deltas keep meters fresh.
        force_full = (wave_n % 16 == 1) or (pid not in _LAST_MAP)
        meters = _delta_meters(pid, usage_map, force_full=force_full)
        listed = len(usage_map)
        if not meters:
            logger.info("report skip panel=%s no-delta listed=%s", pid, listed)
            return
        sent = await _post_meter_chunks(
            pid=pid,
            meters=meters,
            listed=listed,
            dt_ms=int((time.monotonic() - t0) * 1000),
        )
        logger.info(
            "reported panel=%s sent=%s listed=%s full=%s dt=%.1fs",
            pid,
            sent,
            listed,
            int(force_full),
            time.monotonic() - t0,
        )
    finally:
        close = getattr(adapter, "close", None)
        if callable(close):
            try:
                await close()
            except Exception:  # noqa: BLE001
                pass


_WAVE_N = 0


async def _collect_loop() -> None:
    """Every ``_INTERVAL`` seconds: heartbeat + paced usage for all panels."""
    global _WAVE_N
    await asyncio.sleep(3)
    while True:
        t0 = time.monotonic()
        try:
            await _heartbeat_once()
            panels = list(_ASSIGNED)
            if panels:
                _WAVE_N += 1
                wave_n = _WAVE_N
                sem = asyncio.Semaphore(max(1, min(_COLLECT_CONC, len(panels))))

                async def _run(p: dict[str, Any]) -> None:
                    async with sem:
                        try:
                            await _collect_one(p, wave_n=wave_n)
                        except Exception:  # noqa: BLE001
                            logger.warning(
                                "collect failed panel=%s",
                                p.get("id"),
                                exc_info=True,
                            )

                await asyncio.gather(*[_run(p) for p in panels])
                logger.info(
                    "collect wave panels=%s dt=%.1fs interval=%s n=%s",
                    len(panels),
                    time.monotonic() - t0,
                    _INTERVAL,
                    wave_n,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("collect loop error")
        elapsed = time.monotonic() - t0
        await asyncio.sleep(max(30.0, float(_INTERVAL) - elapsed))


@app.on_event("startup")
async def _startup() -> None:
    global _COLLECT_TASK
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info(
        "worker start name=%s master=%s",
        WORKER_NAME,
        MASTER_URL or "(none)",
    )
    _COLLECT_TASK = asyncio.create_task(_collect_loop(), name="usage-collect")
