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
    on_hold_expire_duration: int = 0
    include_promo_first: bool = True
    note: str | None = None
    group_ids: list[int] = Field(default_factory=list)
    # Bind user to a sales category so later category membership changes sync
    category_id: int | None = None
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
    on_hold_expire_duration: int | None = None
    include_promo_first: bool | None = None
    note: str | None = None
    group_ids: list[int] | None = None
    # Set to bind/rebind; null + clear_category clears tracking
    category_id: int | None = None
    clear_category: bool = False
    proxies: dict[str, Any] | None = None
    proxy_settings: dict[str, Any] | None = None
    data_limit_reset_strategy: str | None = None


class PanelAccountOut(ORMModel):
    id: int
    panel_id: int
    group_id: int | None = None
    panel_name: str | None = None
    panel_type: str | None = None
    panel_base_url: str | None = None
    remote_username: str | None
    status: PanelAccountStatus
    links: list[str]
    error_message: str | None
    used_traffic: int
    coeff_anchor_raw: int = 0
    coeff_anchor_weighted: int = 0
    applied_coefficient: float | None = None
    last_sync_at: datetime | None
    configs_count: int = 0


class UserResponse(ORMModel):
    username: str
    display_name: str | None = None
    status: UserStatus
    used_traffic: int = 0
    lifetime_used_traffic: int = 0
    data_limit: int = 0
    expire: int | None = None
    on_hold_expire_duration: int = 0
    include_promo_first: bool = True
    data_limit_reset_strategy: str = "no_reset"
    note: str | None = None
    created_at: datetime | None = None
    links: list[str] = Field(default_factory=list)
    subscription_url: str = ""
    proxies: dict[str, Any] = Field(default_factory=dict)
    group_ids: list[int] = Field(default_factory=list)
    source_category_id: int | None = None
    panel_accounts: list[PanelAccountOut] = Field(default_factory=list)


class UsersListCounts(BaseModel):
    """Global chip totals for the current search/group scope (all pages)."""

    all: int = 0
    active: int = 0
    error: int = 0
    expired: int = 0


class UsersListResponse(BaseModel):
    users: list[UserResponse]
    total: int
    counts: UsersListCounts = Field(default_factory=UsersListCounts)


class UserFromTemplate(BaseModel):
    user_template_id: int
    username: str = Field(min_length=3, max_length=32)
    note: str | None = None


class UserBulkCreate(BaseModel):
    """Create many users: ``{prefix}_1`` … ``{prefix}_{count}``."""

    username_prefix: str = Field(
        min_length=1,
        max_length=28,
        pattern=r"^[a-zA-Z0-9_]+$",
    )
    count: int = Field(ge=1, le=500)
    display_name: str | None = Field(default=None, max_length=64)
    status: UserStatus = UserStatus.active
    data_limit: int = 0
    expire: int | None = 0
    on_hold_expire_duration: int = 0
    include_promo_first: bool = True
    note: str | None = None
    group_ids: list[int] = Field(default_factory=list)
    category_id: int | None = None
    proxies: dict[str, Any] = Field(default_factory=dict)
    proxy_settings: dict[str, Any] = Field(default_factory=dict)
    inbounds: dict[str, Any] = Field(default_factory=dict)
    data_limit_reset_strategy: str = "no_reset"


class UserBulkFail(BaseModel):
    username: str
    message: str


class UserBulkCreateResponse(BaseModel):
    created: list[UserResponse] = Field(default_factory=list)
    failed: list[UserBulkFail] = Field(default_factory=list)
    total_requested: int = 0
    # True when panel remotes keep creating after the HTTP response.
    remote_bg: bool = False
    message: str = ""


class UserRetryErrorsResponse(BaseModel):
    """Bulk retry of error/pending remote accounts across all users."""

    attempted: int = 0
    recovered: int = 0
    remaining: int = 0
    dropped: int = 0
    skipped_unreachable: int = 0
    skipped_capacity: int = 0
    panels_ok: list[str] = Field(default_factory=list)
    panels_down: list[str] = Field(default_factory=list)
    users_recovered: int = 0
    users_remaining: int = 0
    healed_coverage: int = 0
    swept_active: int = 0
    swept_pending: int = 0
    swept_inactive: int = 0


class UserFixSubResponse(BaseModel):
    """One-user heal: retry errors, fill missing panels, hydrate share links."""

    username: str
    before_share: int = 0
    after_share: int = 0
    before_gaps: int = 0
    after_gaps: int = 0
    retried: int = 0
    healed: int = 0
    hydrated: int = 0
    message: str = ""
    user: UserResponse | None = None


class UserCheckUsagePanel(BaseModel):
    panel_id: int
    panel_name: str | None = None
    panel_type: str | None = None
    remote_username: str | None = None
    status: str = ""
    used_raw: int = 0
    last_sync_at: str | None = None


class UserCheckUsageResponse(BaseModel):
    """Live pull of one user's usage from every remote panel."""

    username: str
    used_before: int = 0
    used_after: int = 0
    data_limit: int = 0
    status: str = ""
    accounts: int = 0
    synced: int = 0
    dt_ms: int = 0
    message: str = ""
    panels: list[UserCheckUsagePanel] = Field(default_factory=list)
    user: UserResponse | None = None
