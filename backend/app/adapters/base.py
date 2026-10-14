from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.adapters.http_utils import normalize_panel_base_url


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
