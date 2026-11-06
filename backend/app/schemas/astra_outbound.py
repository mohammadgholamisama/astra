from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AstraOutboundOut(BaseModel):
    id: int
    panel_id: int
    uri: str
    uri_hash: str = ""
    remark: str
    protocol: str
    host: str
    port: int
    is_enabled: bool
    last_ping_ms: int | None = None
    last_ping_ok: bool | None = None
    last_ping_at: datetime | None = None
    sort_order: int = 0
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class AstraOutboundBulkCreate(BaseModel):
    text: str = Field(..., description="One share-link per line")


class AstraOutboundBulkResult(BaseModel):
    created: list[AstraOutboundOut]
    errors: list[str] = []
    total_created: int = 0


class AstraOutboundSubBind(BaseModel):
    """Bind a subscription and attach **one** chosen share-link as this panel's outbound."""

    url: str = Field(..., min_length=8, max_length=2000)
    uri_hash: str = Field(..., min_length=16, max_length=64)
    ping: bool = True


class AstraOutboundSubPreview(BaseModel):
    """Fetch a subscription URL and list pickable configs (no write)."""

    url: str = Field(..., min_length=8, max_length=2000)


class AstraOutboundSubConfigOut(BaseModel):
    index: int
    uri: str
    uri_hash: str
    remark: str
    protocol: str
    host: str
    port: int
    used: bool = False
    used_by_panel_id: int | None = None
    used_by_panel_name: str | None = None


class AstraOutboundSubPreviewOut(BaseModel):
    url: str
    title: str | None = None
    upload: int = 0
    download: int = 0
    total: int = 0
    expire: int = 0
    count: int = 0
    configs: list[AstraOutboundSubConfigOut] = Field(default_factory=list)


class AstraOutboundSubscriptionOut(BaseModel):
    url: str
    title: str | None = None
    upload: int = 0
    download: int = 0
    total: int = 0
    expire: int = 0
    synced_at: str | None = None
    error: str | None = None
    count: int = 0
    selected_hash: str | None = None


class AstraOutboundSubSyncResult(BaseModel):
    ok: bool = True
    created: int = 0
    updated: int = 0
    removed: int = 0
    total: int = 0
    subscription: AstraOutboundSubscriptionOut | None = None
    outbounds: list[AstraOutboundOut] = Field(default_factory=list)


class AstraOutboundEnable(BaseModel):
    is_enabled: bool


class AstraNodeHealth(BaseModel):
    xray_dir: str
    config_exists: bool
    listen_port: int
    listen_host: str = "127.0.0.1"
    public_host: str
    public_port: int
    public_ip: str = ""
    ws_path: str = ""
    inbound: str = "vless+reality"
    reality_sni: str = ""
    listen_open: bool
    dns_ok: bool = True
    dns_note: str = ""
    ok: bool


class AstraNodeClientOut(BaseModel):
    id: int
    panel_id: int
    email: str
    uuid: str
    enable: bool
    data_limit: int = 0
    used_traffic: int = 0
    weighted_used: int = 0
    coefficient: float = 1.0
    limit_ip: int = 0
    expire: int = 0
    on_hold_expire_duration: int = 0
    links_count: int = 0
    share_links: list[str] = []
    subscription_url: str | None = None
    user_id: int | None = None
    astra_username: str | None = None
    user_status: str | None = None
    group_id: int | None = None
    group_name: str | None = None
    account_status: str | None = None
    account_used_traffic: int | None = None
    created_at: datetime | None = None


class AstraNodeClientEnable(BaseModel):
    enable: bool
