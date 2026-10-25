from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AstraOutboundOut(BaseModel):
    id: int
    panel_id: int
    uri: str
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
