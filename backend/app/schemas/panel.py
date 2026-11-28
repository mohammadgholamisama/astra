from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.panel import PanelType
from app.schemas.common import ORMModel


class PanelCreate(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    type: PanelType = Field(
        description=(
            "نوع پنل — مقدار دقیق از `GET /api/v1/panel-types` "
            "(مثلاً sanaei / pasarguard / marzban / rebecca / guard / astra)."
        ),
    )
    base_url: str
    username: str | None = None
    password: str | None = None
    api_token: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True
    category_id: int | None = None


class PanelUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    username: str | None = None
    password: str | None = None
    api_token: str | None = None
    meta: dict[str, Any] | None = None
    is_active: bool | None = None
    category_id: int | None = None
    clear_category: bool = False


class PanelActiveSet(BaseModel):
    """Toggle panel on/off without re-probing credentials."""

    is_active: bool


class PanelTestOut(BaseModel):
    id: int
    name: str
    type: PanelType
    is_active: bool
    success: bool
    error: str | None = None
    # Partial Astra hops: success may still be True while some outbounds are cut.
    warning: str | None = None
    latency_ms: int | None = None
    grade: str | None = None  # fast | ok | slow | fail
    base_url: str | None = None
    worker_id: int | None = None
    worker_name: str | None = None
    via_worker: bool = False


class PanelTestAllStats(BaseModel):
    total: int = 0
    ok: int = 0
    fail: int = 0
    active_ok: int = 0
    active_fail: int = 0
    inactive_ok: int = 0
    inactive_fail: int = 0
    fast: int = 0
    normal: int = 0
    slow: int = 0
    avg_latency_ms: int | None = None
    median_latency_ms: int | None = None
    best_latency_ms: int | None = None
    worst_latency_ms: int | None = None
    duration_ms: int = 0


class PanelTestAllBody(BaseModel):
    """Scope test-all to the panels the operator currently sees (tab/search)."""

    panel_ids: list[int] | None = None


class PanelTestAllOut(BaseModel):
    results: list[PanelTestOut] = Field(default_factory=list)
    ok: int = 0
    fail: int = 0
    stats: PanelTestAllStats = Field(default_factory=PanelTestAllStats)
    tested_at: datetime | None = None


class PanelOut(ORMModel):
    id: int
    name: str
    type: PanelType
    base_url: str
    username: str | None
    has_password: bool = False
    has_token: bool = False
    meta: dict[str, Any]
    is_active: bool
    category_id: int | None = None
    sort_order: int = 0
    worker_id: int | None = None
    last_ok_at: datetime | None
    created_at: datetime


class PanelReorderBody(BaseModel):
    """Reorder panels inside one UI tab (category or «همه پنل‌ها»)."""

    panel_ids: list[int] = Field(min_length=1)
    category_id: int | None = None


class PanelCategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=48)
    sort_order: int = 0


class PanelCategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=48)
    sort_order: int | None = None


class PanelCategoryOut(ORMModel):
    id: int
    name: str
    sort_order: int
    panels_count: int = 0
    created_at: datetime


class PanelMoveBody(BaseModel):
    """Move a panel into a category tab, or null for default «همه پنل‌ها»."""

    category_id: int | None = None


class InboundOut(BaseModel):
    id: int
    remark: str = ""
    protocol: str = ""
    port: int | None = None
    enable: bool = True


class InboundPreviewRequest(BaseModel):
    base_url: str
    username: str | None = None
    password: str | None = None
    api_token: str = Field(min_length=1)


class RemoteGroupOut(BaseModel):
    id: int
    name: str = ""
    is_disabled: bool = False


class GroupPreviewRequest(BaseModel):
    base_url: str
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    api_token: str | None = None


class MarzInboundOut(BaseModel):
    tag: str
    protocol: str
    network: str = ""
    tls: str = ""
    port: int | str | None = None


class MarzProtocolOut(BaseModel):
    protocol: str
    tags: list[str] = Field(default_factory=list)
    inbound_count: int = 0
    inbounds: list[MarzInboundOut] = Field(default_factory=list)
    # Rebecca services (exposed via list_protocols)
    service_id: int | None = None
    service_name: str | None = None
    is_disabled: bool = False


class MarzInboundPreviewRequest(BaseModel):
    base_url: str
    username: str = ""
    password: str = ""
    api_token: str | None = None
    panel_type: PanelType = PanelType.marzban
