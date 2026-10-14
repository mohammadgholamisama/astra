from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    group_ids: list[int] = Field(min_length=1)
    data_limit: int = 0
    expire_duration: int = 0
    username_prefix: str | None = None
    username_suffix: str | None = None
    status: str = "active"
    data_limit_reset_strategy: str = "no_reset"
    is_disabled: bool = False
    extra_settings: dict = Field(default_factory=dict)
    note: str | None = None


class TemplateUpdate(BaseModel):
    name: str | None = None
    group_ids: list[int] | None = None
    data_limit: int | None = None
    expire_duration: int | None = None
    username_prefix: str | None = None
    username_suffix: str | None = None
    status: str | None = None
    data_limit_reset_strategy: str | None = None
    is_disabled: bool | None = None
    extra_settings: dict | None = None
    note: str | None = None


class TemplateOut(ORMModel):
    id: int
    name: str
    group_ids: list[int]
    data_limit: int
    expire_duration: int
    username_prefix: str | None
    username_suffix: str | None
    status: str
    data_limit_reset_strategy: str
    is_disabled: bool
    extra_settings: dict
    note: str | None
    created_at: datetime
