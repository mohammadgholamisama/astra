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
    notices: list[str] = Field(default_factory=list, max_length=10)
    panels: list[GroupPanelLink] = Field(default_factory=list)
    inbound_tags: list[str] = Field(default_factory=list)  # PasarGuard compat field (ignored/stored in desc)


class GroupUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_disabled: bool | None = None
    notices: list[str] | None = Field(default=None, max_length=10)
    panels: list[GroupPanelLink] | None = None
    inbound_tags: list[str] | None = None


class GroupPanelOut(ORMModel):
    panel_id: int
    config: dict[str, Any]
    priority: int
    panel_name: str | None = None
    panel_type: str | None = None


class GroupOut(ORMModel):
    id: int
    name: str
    description: str | None
    is_disabled: bool
    notices: list[str] = Field(default_factory=list)
    inbound_tags: list[str] = Field(default_factory=list)
    panels: list[GroupPanelOut] = Field(default_factory=list)
    created_at: datetime
