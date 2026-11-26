"""Public integration schemas for /docs (X-Api-Key)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class IntegrationGroupPanelIn(BaseModel):
    panel_id: int = Field(..., description="شناسه پنل Astra")
    config: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0


class IntegrationGroupCreate(BaseModel):
    name: str = Field(..., min_length=3, max_length=64, description="نام یکتای گروه")
    description: str | None = None
    is_disabled: bool = False
    config_tag: str = Field(default="", max_length=200)
    usage_cost: int | None = Field(default=None, ge=0, le=1_000_000)
    order_kind: str = Field(
        default="other",
        description="direct | tunnel | gaming | other",
    )
    notices: list[str] = Field(default_factory=list)
    category_id: int | None = None
    panels: list[IntegrationGroupPanelIn] = Field(default_factory=list)


class IntegrationGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=3, max_length=64)
    description: str | None = None
    is_disabled: bool | None = None
    config_tag: str | None = None
    usage_cost: int | None = Field(default=None, ge=0, le=1_000_000)
    clear_usage_cost: bool | None = None
    order_kind: str | None = None
    notices: list[str] | None = None
    category_id: int | None = None
    clear_category: bool | None = None
    panels: list[IntegrationGroupPanelIn] | None = None


class IntegrationGroupPanelOut(BaseModel):
    panel_id: int
    panel_name: str | None = None
    panel_type: str | None = None
    panel_is_active: bool = True
    priority: int = 0
    config: dict[str, Any] = Field(default_factory=dict)


class IntegrationGroupOut(BaseModel):
    id: int
    name: str
    description: str | None = None
    is_disabled: bool = False
    config_tag: str = ""
    usage_cost: int | None = None
    order_kind: str = "other"
    notices: list[str] = Field(default_factory=list)
    category_id: int | None = None
    sort_order: int = 0
    panels: list[IntegrationGroupPanelOut] = Field(default_factory=list)
    created_at: datetime | None = None


class PanelConfigTestOut(BaseModel):
    """One share-link fingerprint belonging to a panel."""

    host: str
    port: int
    match_host: str = ""
    label: str | None = None
    protocol: str | None = None
    sub_rank: int = Field(
        ...,
        description="رتبه ۱-based در ترتیب سراسری ساب (عدد کوچک‌تر = بالاتر در /sub)",
    )
    avg_ping_ms: float | None = Field(
        default=None, description="میانگین پینگ چنددستگاهی (ms)؛ null = هنوز تست نشده"
    )
    device_count: int | None = None
    tested: bool = False
    hidden: bool = False
    group_names: list[str] = Field(default_factory=list)
    category_name: str | None = None
    flag: str | None = None


class PanelSubStatusOut(BaseModel):
    panel_id: int
    name: str
    type: str
    is_active: bool
    worker_id: int | None = None
    worker_name: str | None = None
    configs_count: int = 0
    tested_count: int = 0
    best_ping_ms: float | None = None
    worst_ping_ms: float | None = None
    best_sub_rank: int | None = Field(
        default=None, description="بهترین (کوچک‌ترین) رتبهٔ کانفیگ این پنل در ساب"
    )
    configs: list[PanelConfigTestOut] = Field(default_factory=list)


class PanelsSubStatusResponse(BaseModel):
    total_panels: int
    total_ordered_configs: int
    panels: list[PanelSubStatusOut]
