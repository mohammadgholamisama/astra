from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.adapters.http_utils import normalize_panel_base_url


def coerce_unix_ts(value: Any) -> int:
    """Best-effort unix seconds. 0 if missing, unlimited, or a hold-duration."""
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        n = int(value)
        if n < 0:
            return 0
        # 3x-ui expiryTime / JS timestamps are milliseconds
        if n > 10_000_000_000:
            n //= 1000
        return n if n > 0 else 0
    if isinstance(value, str):
        s = value.strip()
        if not s or s.lower() in ("0", "null", "none", "undefined"):
            return 0
        try:
            return coerce_unix_ts(float(s))
        except (TypeError, ValueError):
            pass
        try:
            iso = s[:-1] + "+00:00" if s.endswith("Z") else s
            return int(datetime.fromisoformat(iso).timestamp())
        except Exception:  # noqa: BLE001
            return 0
    return 0


def apply_remote_state(result: "AdapterResult", data: dict | None) -> "AdapterResult":
    """Copy status / expire / online_at from a Marzban-family user payload."""
    if not isinstance(data, dict):
        return result
    st = data.get("status")
    if st:
        result.remote_status = str(st).strip().lower()
    exp = coerce_unix_ts(data.get("expire"))
    if exp:
        result.remote_expire = exp
    online = coerce_unix_ts(
        data.get("online_at")
        or data.get("last_online")
        or data.get("onlineAt")
        or data.get("last_online_at")
    )
    if online:
        result.online_at = online
    return result


@dataclass
class UserPayload:
    username: str
    data_limit: int = 0  # bytes
    expire: int | None = None  # unix ts
    status: str = "active"
    note: str | None = None
    uuid: str | None = None
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class AdapterResult:
    success: bool
    remote_username: str | None = None
    remote_id: str | None = None
    uuid: str | None = None
    links: list[str] = field(default_factory=list)
    used_traffic: int = 0
    error: str | None = None
    raw: Any = None
    # Live remote clock — used to detect on_hold self-activation on first connect
    remote_status: str | None = None
    remote_expire: int | None = None  # unix seconds; 0/None = none / still on_hold
    online_at: int | None = None


class BasePanelAdapter(ABC):
    def __init__(self, base_url: str, username: str | None, password: str | None, api_token: str | None, meta: dict | None = None):
        self.base_url = normalize_panel_base_url(base_url)
        self.username = (username or "").strip() or None
        self.password = (password or "").strip() or None
        tok = (api_token or "").strip()
        if tok.lower().startswith("bearer "):
            tok = tok[7:].strip()
        self.api_token = tok or None
        self.meta = meta or {}

    @abstractmethod
    async def test_connection(self) -> AdapterResult: ...

    @abstractmethod
    async def create_user(self, payload: UserPayload) -> AdapterResult: ...

    @abstractmethod
    async def modify_user(self, payload: UserPayload) -> AdapterResult: ...

    @abstractmethod
    async def delete_user(self, username: str, remote_id: str | None = None) -> AdapterResult: ...

    @abstractmethod
    async def get_usage(self, username: str, remote_id: str | None = None) -> AdapterResult: ...

    @abstractmethod
    async def get_subscription_links(self, username: str, remote_id: str | None = None) -> AdapterResult: ...

    async def reset_usage(self, username: str, remote_id: str | None = None) -> AdapterResult:
        """Reset used traffic on the remote panel. Default: unsupported (local-only)."""
        return AdapterResult(
            success=False,
            error="reset_usage not supported on this panel type",
            remote_username=username,
            remote_id=remote_id,
        )
