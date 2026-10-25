from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class GroupConfigItemOut(ORMModel):
    id: int
    group_id: int
    panel_id: int
    panel_name: str | None = None
    panel_type: str | None = None
    config_key: str
    label: str
    protocol: str
    sample_link: str | None = None
    coefficient: float = 1.0
    ip_limit_enabled: bool = False
    ip_limit: int = 0
    sort_order: int = 0
    updated_at: datetime | None = None
    created_at: datetime | None = None


class GroupConfigItemUpdate(BaseModel):
    id: int
    coefficient: float = Field(default=1.0, ge=0.01)
    ip_limit_enabled: bool = False
    ip_limit: int = Field(default=0, ge=0, le=10000)
    label: str | None = None


class GroupConfigBulkUpdate(BaseModel):
    items: list[GroupConfigItemUpdate]


class GroupConfigSampleAccountOut(BaseModel):
    panel_id: int
    panel_name: str | None = None
    panel_type: str | None = None
    status: str
    links_count: int = 0
    error_message: str | None = None


class GroupConfigWorkspaceOut(BaseModel):
    group_id: int
    group_name: str
    sample_username: str | None = None
    sample_ready: bool = False
    accounts: list[GroupConfigSampleAccountOut] = Field(default_factory=list)
    items: list[GroupConfigItemOut] = Field(default_factory=list)
