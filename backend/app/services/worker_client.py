"""HTTP client + health cache for remote Astra workers."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from typing import Any

import httpx

from app.adapters.base import AdapterResult, UserPayload
from app.adapters.http_utils import normalize_panel_base_url

logger = logging.getLogger("astra.worker")

# worker_id → (ok: bool, checked_at monotonic, error)
_HEALTH: dict[int, tuple[bool, float, str]] = {}
_HEALTH_TTL_OK = 20.0
_HEALTH_TTL_BAD = 8.0
_RAW_TOKENS: dict[int, str] = {}  # optional in-memory after create (process lifetime)
# worker_id → (base_url, token, active) — sync lookup for get_adapter()
_ROUTE: dict[int, tuple[str, str, bool]] = {}

# One long-lived AsyncClient — reuse TLS/Keep-Alive to remote workers.
_HTTP: httpx.AsyncClient | None = None
_HTTP_LOCK = asyncio.Lock()


async def _shared_http() -> httpx.AsyncClient:
    global _HTTP
    if _HTTP is not None and not _HTTP.is_closed:
        return _HTTP
    async with _HTTP_LOCK:
        if _HTTP is not None and not _HTTP.is_closed:
            return _HTTP
        _HTTP = httpx.AsyncClient(
            # 6s connect / 10s read — one slow tunnel must not stall a 12s batch.
            timeout=httpx.Timeout(10.0, connect=6.0),
            verify=False,
            limits=httpx.Limits(
                max_connections=40,
                max_keepalive_connections=20,
                keepalive_expiry=60.0,
            ),
            http2=False,
        )
        return _HTTP


async def close_shared_http() -> None:
    global _HTTP
    if _HTTP is not None:
        try:
            await _HTTP.aclose()
        except Exception:  # noqa: BLE001
            pass
        _HTTP = None


def register_worker_route(
    worker_id: int, base_url: str, token: str, *, active: bool = True
) -> None:
    wid = int(worker_id)
    tok = (token or "").strip()
    if not tok:
        _ROUTE.pop(wid, None)
        return
    _ROUTE[wid] = (
        normalize_panel_base_url(base_url),
        tok,
        bool(active),
    )
    remember_raw_token(wid, tok)


def lookup_worker_route(worker_id: int) -> tuple[str, str] | None:
    row = _ROUTE.get(int(worker_id))
    if not row:
        return None
    base, token, active = row
    if not active or not token:
        return None
    return base, token


def seed_worker_route_from_row(worker: Any, *, token: str | None = None) -> bool:
    """Register route from a WorkerNode ORM/namespace row. Returns True if live."""
    try:
        wid = int(getattr(worker, "id", 0) or 0)
    except (TypeError, ValueError):
        return False
    if wid <= 0:
        return False
    if not bool(getattr(worker, "is_active", False)):
        drop_worker_route(wid)
        return False
    tok = (token or "").strip()
    if not tok:
        tok = str((getattr(worker, "meta", None) or {}).get("agent_token") or "").strip()
    if not tok:
        tok = (peek_raw_token(wid) or "").strip()
    if not tok:
        return lookup_worker_route(wid) is not None
    register_worker_route(
        wid,
        str(getattr(worker, "base_url", "") or ""),
        tok,
        active=True,
    )
    return lookup_worker_route(wid) is not None


def drop_worker_route(worker_id: int) -> None:
    _ROUTE.pop(int(worker_id), None)
    _RAW_TOKENS.pop(int(worker_id), None)
    _HEALTH.pop(int(worker_id), None)


def hash_worker_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def new_worker_token() -> str:
    return secrets.token_urlsafe(32)


def remember_raw_token(worker_id: int, token: str) -> None:
    _RAW_TOKENS[int(worker_id)] = token


def peek_raw_token(worker_id: int) -> str | None:
    return _RAW_TOKENS.get(int(worker_id))


def mark_worker_health(worker_id: int, ok: bool, error: str = "") -> None:
    _HEALTH[int(worker_id)] = (bool(ok), time.monotonic(), (error or "")[:400])


def worker_health_cached(worker_id: int) -> tuple[bool | None, str]:
    """Return (ok|None unknown, error)."""
    row = _HEALTH.get(int(worker_id))
    if not row:
        return None, ""
    ok, ts, err = row
    ttl = _HEALTH_TTL_OK if ok else _HEALTH_TTL_BAD
    if time.monotonic() - ts > ttl:
        return None, err
    return ok, err


def any_worker_healthy() -> bool:
    """True if at least one worker is cached healthy (master can ease fleet)."""
    now = time.monotonic()
    for ok, ts, _err in _HEALTH.values():
        if not ok:
            continue
        if now - ts <= _HEALTH_TTL_OK:
            return True
    return False


def panel_snapshot(panel: Any) -> dict[str, Any]:
    ptype = getattr(panel, "type", None)
    ptype_s = ptype.value if hasattr(ptype, "value") else str(ptype or "")
    meta = dict(getattr(panel, "meta", None) or {})
    if getattr(panel, "id", None) is not None:
        meta["panel_id"] = int(panel.id)
    return {
        "id": int(getattr(panel, "id", 0) or 0),
        "type": ptype_s,
        "base_url": normalize_panel_base_url(str(getattr(panel, "base_url", "") or "")),
        "username": getattr(panel, "username", None),
        "password": getattr(panel, "password", None),
        "api_token": getattr(panel, "api_token", None)
        or str(meta.get("node_token") or "")
        or None,
        "meta": meta,
    }


def _result_from_json(data: dict[str, Any]) -> AdapterResult:
    return AdapterResult(
        success=bool(data.get("success")),
        remote_username=data.get("remote_username"),
        remote_id=data.get("remote_id"),
        uuid=data.get("uuid"),
        links=[str(x) for x in (data.get("links") or []) if x],
        used_traffic=int(data.get("used_traffic") or 0),
        error=data.get("error"),
        raw=data.get("raw"),
        remote_status=data.get("remote_status"),
        remote_expire=data.get("remote_expire"),
        online_at=data.get("online_at"),
    )


def _payload_dict(payload: UserPayload) -> dict[str, Any]:
    return {
        "username": payload.username,
        "data_limit": int(payload.data_limit or 0),
        "expire": payload.expire,
        "status": payload.status,
        "note": payload.note,
        "uuid": payload.uuid,
        "config": dict(payload.config or {}),
    }


async def ping_worker(
    *,
    base_url: str,
    token: str,
    timeout: float = 4.0,
) -> tuple[bool, str]:
    url = normalize_panel_base_url(base_url).rstrip("/") + "/health"
    try:
        client = await _shared_http()
        res = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        if res.status_code == 200:
            return True, ""
        return False, f"HTTP {res.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:300]


async def exec_on_worker(
    *,
    base_url: str,
    token: str,
    op: str,
    panel: Any,
    args: dict[str, Any] | None = None,
    timeout: float = 45.0,
) -> AdapterResult:
    url = normalize_panel_base_url(base_url).rstrip("/") + "/v1/exec"
    body = {
        "op": op,
        "panel": panel_snapshot(panel),
        "args": args or {},
    }
    try:
        client = await _shared_http()
        res = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=timeout,
        )
        if res.status_code != 200:
            return AdapterResult(
                success=False,
                error=f"worker HTTP {res.status_code}: {(res.text or '')[:240]}",
            )
        data = res.json()
        if not isinstance(data, dict):
            return AdapterResult(success=False, error="worker bad json")
        return _result_from_json(data)
    except Exception as exc:  # noqa: BLE001
        return AdapterResult(success=False, error=f"worker unreachable: {exc}"[:300])


async def push_assignments(
    *,
    base_url: str,
    token: str,
    master_url: str,
    panels: list[dict[str, Any]],
    interval_sec: int = 30,
    timeout: float = 12.0,
) -> tuple[bool, str]:
    url = normalize_panel_base_url(base_url).rstrip("/") + "/v1/assign"
    body = {
        "master_url": master_url.rstrip("/"),
        "interval_sec": int(interval_sec),
        "panels": panels,
    }
    try:
        client = await _shared_http()
        res = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=timeout,
        )
        if res.status_code == 200:
            return True, ""
        return False, f"HTTP {res.status_code}: {(res.text or '')[:200]}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:300]


class WorkerRoutedAdapter:
    """Proxy adapter: try remote worker, fall back to local adapter."""

    def __init__(
        self,
        panel: Any,
        local: Any,
        *,
        worker_id: int,
        worker_base_url: str,
        worker_token: str,
        op_timeout: float = 40.0,
    ) -> None:
        self._panel = panel
        self._local = local
        self._worker_id = int(worker_id)
        self._base_url = worker_base_url
        self._token = worker_token
        self._op_timeout = float(op_timeout)
        # Match BasePanelAdapter attrs used elsewhere
        self.base_url = getattr(local, "base_url", "")
        self.username = getattr(local, "username", None)
        self.password = getattr(local, "password", None)
        self.api_token = getattr(local, "api_token", None)
        self.meta = getattr(local, "meta", {}) or {}

    async def close(self) -> None:
        close = getattr(self._local, "close", None)
        if callable(close):
            try:
                await close()
            except Exception:  # noqa: BLE001
                pass

    async def _via_worker(self, op: str, args: dict[str, Any]) -> AdapterResult | None:
        cached, _err = worker_health_cached(self._worker_id)
        if cached is False:
            return None
        res = await exec_on_worker(
            base_url=self._base_url,
            token=self._token,
            op=op,
            panel=self._panel,
            args=args,
            timeout=self._op_timeout,
        )
        err = (res.error or "").strip()
        # Only transport / 5xx means agent down. 4xx = bad op/payload → local
        # fallback without marking the whole worker offline (was freezing create).
        if err.startswith("worker unreachable"):
            mark_worker_health(self._worker_id, False, err)
            logger.warning(
                "worker failover local worker=%s op=%s err=%s",
                self._worker_id,
                op,
                err[:160],
            )
            return None
        if err.startswith("worker HTTP 5") or err.startswith("worker HTTP 502") or err.startswith("worker HTTP 503"):
            mark_worker_health(self._worker_id, False, err)
            logger.warning(
                "worker failover local worker=%s op=%s err=%s",
                self._worker_id,
                op,
                err[:160],
            )
            return None
        if err.startswith("worker HTTP 4"):
            # Unsupported type / bad request — use master, keep worker healthy.
            # Do NOT mark offline: that would freeze all create/delete/usage.
            logger.info(
                "worker skip→local worker=%s op=%s err=%s",
                self._worker_id,
                op,
                err[:120],
            )
            return None
        # Real adapter outcome (success or panel error) — never double-create
        # on master when worker already answered.
        mark_worker_health(self._worker_id, True, "")
        return res

    async def test_connection(self) -> AdapterResult:
        got = await self._via_worker("test_connection", {})
        if got is not None:
            return got
        return await self._local.test_connection()

    async def create_user(self, payload: UserPayload) -> AdapterResult:
        got = await self._via_worker(
            "create_user", {"payload": _payload_dict(payload)}
        )
        if got is not None:
            return got
        return await self._local.create_user(payload)

    async def modify_user(self, payload: UserPayload) -> AdapterResult:
        got = await self._via_worker(
            "modify_user", {"payload": _payload_dict(payload)}
        )
        if got is not None:
            return got
        return await self._local.modify_user(payload)

    async def delete_user(
        self, username: str, remote_id: str | None = None
    ) -> AdapterResult:
        got = await self._via_worker(
            "delete_user",
            {"username": username, "remote_id": remote_id},
        )
        if got is not None:
            return got
        return await self._local.delete_user(username, remote_id)

    async def get_usage(
        self,
        username: str,
        remote_id: str | None = None,
        expand_links: bool = True,
        **kwargs: Any,
    ) -> AdapterResult:
        args = {
            "username": username,
            "remote_id": remote_id,
            "expand_links": bool(expand_links),
            **kwargs,
        }
        # Bound usage hops tightly — worker serves meter_cache in <100ms when fresh.
        prev_to = self._op_timeout
        self._op_timeout = min(prev_to, 12.0)
        try:
            got = await self._via_worker("get_usage", args)
        finally:
            self._op_timeout = prev_to
        if got is not None:
            return got
        try:
            return await self._local.get_usage(
                username, remote_id, expand_links=expand_links
            )
        except TypeError:
            return await self._local.get_usage(username, remote_id)

    async def get_subscription_links(
        self, username: str, remote_id: str | None = None
    ) -> AdapterResult:
        got = await self._via_worker(
            "get_subscription_links",
            {"username": username, "remote_id": remote_id},
        )
        if got is not None:
            return got
        return await self._local.get_subscription_links(username, remote_id)

    async def reset_usage(
        self, username: str, remote_id: str | None = None
    ) -> AdapterResult:
        got = await self._via_worker(
            "reset_usage",
            {"username": username, "remote_id": remote_id},
        )
        if got is not None:
            return got
        return await self._local.reset_usage(username, remote_id)

    # ── Panel-edit helpers (not on worker wire protocol) ─────────────────
    # Groups / inbounds / protocols are admin UI reads. Worker agent only
    # speaks create/modify/delete/usage — so always hit the local adapter.
    async def list_groups(self) -> list[dict[str, Any]]:
        fn = getattr(self._local, "list_groups", None)
        if not callable(fn):
            raise AttributeError(
                f"{type(self._local).__name__} has no list_groups"
            )
        return await fn()

    async def list_inbounds(self) -> list[dict[str, Any]]:
        fn = getattr(self._local, "list_inbounds", None)
        if not callable(fn):
            raise AttributeError(
                f"{type(self._local).__name__} has no list_inbounds"
            )
        return await fn()

    async def list_protocols(self) -> list[dict[str, Any]]:
        fn = getattr(self._local, "list_protocols", None)
        if not callable(fn):
            raise AttributeError(
                f"{type(self._local).__name__} has no list_protocols"
            )
        return await fn()

    def __getattr__(self, name: str) -> Any:
        """Forward unknown attrs/methods to the concrete panel adapter."""
        return getattr(self._local, name)


# Avoid unused import warning in type checkers
_ = asyncio
