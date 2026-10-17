from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class GroupPanelLink(BaseModel):
    panel_id: int
    config: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0


class GroupCreate(BaseModel):
    name: str = Field(min_length=3, max_length=64)
    description: str | None = None
    is_disabled: bool = False
    config_tag: str = Field(default="", max_length=24)
    notices: list[str] = Field(default_factory=list, max_length=10)
    # None = default «همه گروه‌ها» bucket
    category_id: int | None = None
    panels: list[GroupPanelLink] = Field(default_factory=list)
    inbound_tags: list[str] = Field(default_factory=list)  # PasarGuard compat field (ignored/stored in desc)


class GroupUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_disabled: bool | None = None
    config_tag: str | None = Field(default=None, max_length=24)
    notices: list[str] | None = Field(default=None, max_length=10)
    category_id: int | None = None
    # Explicit clear: send true to move group back to «همه گروه‌ها»
    clear_category: bool | None = None
    panels: list[GroupPanelLink] | None = None
    inbound_tags: list[str] | None = None


class GroupPanelOut(ORMModel):
    panel_id: int
    config: dict[str, Any]
    priority: int
    panel_name: str | None = None
    panel_type: str | None = None
    panel_is_active: bool = True


class GroupOut(ORMModel):
    id: int
    name: str
    description: str | None
    is_disabled: bool
    config_tag: str = ""
    notices: list[str] = Field(default_factory=list)
    category_id: int | None = None
    inbound_tags: list[str] = Field(default_factory=list)
    panels: list[GroupPanelOut] = Field(default_factory=list)
    created_at: datetime


class GroupCategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=48)
    sort_order: int = 0


class GroupCategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=48)
    sort_order: int | None = None


class GroupCategoryOut(ORMModel):
    id: int
    name: str
    sort_order: int
    groups_count: int = 0
    created_at: datetime


class GroupMoveBody(BaseModel):
    """Move a sales group into a category, or null for default bucket."""

    category_id: int | None = None
