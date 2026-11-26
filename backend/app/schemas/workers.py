from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class WorkerCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=64)
    # Auto-install: give IP + SSH creds — master installs everything.
    ssh_host: str = Field(..., min_length=3, max_length=255, description="IP یا hostname")
    ssh_user: str = Field("root", max_length=64)
    ssh_password: str = Field(..., min_length=1, max_length=256)
    ssh_port: int = Field(22, ge=1, le=65535)
    listen_port: int = Field(2090, ge=1, le=65535)
    # Optional override; default http://{ssh_host}:{listen_port}
    base_url: str | None = Field(None, max_length=512)
    panel_ids: list[int] = Field(default_factory=list)
    # If true (default), SSH in and install agent immediately
    auto_install: bool = True


class WorkerBulkItem(BaseModel):
    """One VPS in a multi-add request."""

    name: str = Field(..., min_length=2, max_length=64)
    ssh_host: str = Field(..., min_length=3, max_length=255)
    ssh_user: str = Field("root", max_length=64)
    ssh_password: str = Field(..., min_length=1, max_length=256)
    ssh_port: int = Field(22, ge=1, le=65535)
    listen_port: int = Field(2090, ge=1, le=65535)


class WorkerBulkCreate(BaseModel):
    """Create + auto-install several workers, then optionally balance panels."""

    workers: list[WorkerBulkItem] = Field(..., min_length=1, max_length=20)
    auto_install: bool = True
    # Spread active non-Astra panels across existing+new online workers.
    balance_panels: bool = True


class WorkerBulkFail(BaseModel):
    name: str
    ssh_host: str = ""
    message: str


class WorkerBulkCreateResponse(BaseModel):
    created: list[WorkerOut] = Field(default_factory=list)
    failed: list[WorkerBulkFail] = Field(default_factory=list)
    balanced: dict[str, list[int]] = Field(default_factory=dict)
    message: str = ""


class WorkerProbeResult(BaseModel):
    """Accurate connectivity report for one worker (agent + optional SSH)."""

    ok: bool = False
    worker_id: int
    name: str = ""
    status: str = "unknown"
    agent_ok: bool = False
    agent_error: str | None = None
    agent_latency_ms: int | None = None
    ssh_tcp_ok: bool | None = None
    ssh_ok: bool | None = None
    ssh_error: str | None = None
    last_error: str | None = None
    message: str = ""
    worker: WorkerOut | None = None


class WorkerUpdate(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=64)
    base_url: str | None = Field(None, min_length=8, max_length=512)
    ssh_host: str | None = Field(None, max_length=255)
    ssh_user: str | None = Field(None, max_length=64)
    # Optional: verify SSH while saving (password never stored).
    ssh_password: str | None = Field(None, max_length=256)
    ssh_port: int | None = Field(None, ge=1, le=65535)
    listen_port: int | None = Field(None, ge=1, le=65535)
    is_active: bool | None = None
    panel_ids: list[int] | None = None
    clear_ssh_host: bool = False
    clear_ssh_user: bool = False


class WorkerReinstall(BaseModel):
    ssh_password: str = Field(..., min_length=1, max_length=256)
    ssh_port: int | None = Field(None, ge=1, le=65535)


class WorkerPanelBrief(BaseModel):
    id: int
    name: str
    type: str
    is_active: bool


class WorkerOut(BaseModel):
    id: int
    name: str
    base_url: str
    is_active: bool
    status: str
    last_seen_at: datetime | None = None
    last_error: str | None = None
    ssh_host: str | None = None
    ssh_user: str | None = None
    listen_port: int = 2090
    panels: list[WorkerPanelBrief] = Field(default_factory=list)
    panel_ids: list[int] = Field(default_factory=list)
    created_at: datetime | None = None
    # Only set on create / rotate — never stored again
    token: str | None = None
    install_command: str | None = None
    install_ok: bool | None = None
    install_log: str | None = None
    message: str | None = None


class WorkerAssignPanels(BaseModel):
    panel_ids: list[int] = Field(default_factory=list)


class WorkerUsageMeter(BaseModel):
    remote_username: str
    used_traffic: int = 0
    links: list[str] | None = None
    status: str | None = None


class WorkerUsageReport(BaseModel):
    panel_id: int
    meters: list[WorkerUsageMeter] = Field(default_factory=list)
    listed: int = 0
    worker_id: int | None = None
    dt_ms: int | None = None
    # Periodic full snapshot — allow meters to shrink (remote reset / delete).
    full_sync: bool = False
    # Worker collect mode: hot | warm | cold (priority-probe posts hot).
    mode: str | None = None


class WorkerHeartbeatIn(BaseModel):
    version: str | None = None
    panels: list[int] = Field(default_factory=list)
    load: dict | None = None
