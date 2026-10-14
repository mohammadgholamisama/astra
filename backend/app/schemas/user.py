from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.user import PanelAccountStatus, UserStatus
from app.schemas.common import ORMModel


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    display_name: str | None = Field(default=None, max_length=64)
    status: UserStatus = UserStatus.active
    data_limit: int = 0
    expire: int | None = 0
    note: str | None = None
    group_ids: list[int] = Field(default_factory=list)
    proxies: dict[str, Any] = Field(default_factory=dict)
    proxy_settings: dict[str, Any] = Field(default_factory=dict)
    inbounds: dict[str, Any] = Field(default_factory=dict)
    data_limit_reset_strategy: str = "no_reset"


class UserModify(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)
    clear_display_name: bool = False
    status: UserStatus | None = None
    data_limit: int | None = None
    expire: int | None = None
    note: str | None = None
    group_ids: list[int] | None = None
    proxies: dict[str, Any] | None = None
    proxy_settings: dict[str, Any] | None = None
    data_limit_reset_strategy: str | None = None


class PanelAccountOut(ORMModel):
    id: int
    panel_id: int
    panel_name: str | None = None
    panel_type: str | None = None
    remote_username: str | None
    status: PanelAccountStatus
    links: list[str]
    error_message: str | None
    used_traffic: int
    coeff_anchor_raw: int = 0
    coeff_anchor_weighted: int = 0
    applied_coefficient: float | None = None
    last_sync_at: datetime | None


class UserResponse(ORMModel):
    username: str
    display_name: str | None = None
    status: UserStatus
    used_traffic: int = 0
    lifetime_used_traffic: int = 0
    data_limit: int = 0
    expire: int | None = None
    data_limit_reset_strategy: str = "no_reset"
    note: str | None = None
    created_at: datetime | None = None
    links: list[str] = Field(default_factory=list)
    subscription_url: str = ""
    proxies: dict[str, Any] = Field(default_factory=dict)
    group_ids: list[int] = Field(default_factory=list)
    panel_accounts: list[PanelAccountOut] = Field(default_factory=list)


class UsersListResponse(BaseModel):
    users: list[UserResponse]
    total: int


class UserFromTemplate(BaseModel):
    user_template_id: int
    username: str = Field(min_length=3, max_length=32)
    note: str | None = None
