from datetime import datetime

from pydantic import BaseModel, Field


class ConfigLocationCreate(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    country_code: str = Field(min_length=2, max_length=2)
    # Optional header/SNI host from link (host=/sni=). Empty = match domain+port only.
    match_host: str | None = Field(default=None, max_length=253)
    label: str | None = Field(default=None, max_length=64)


class ConfigLocationUpdate(BaseModel):
    host: str | None = Field(default=None, min_length=1, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    match_host: str | None = Field(default=None, max_length=253)
    label: str | None = Field(default=None, max_length=64)


class ConfigLocationEndpointSet(BaseModel):
    """Upsert/clear flag for an exact config fingerprint from the Configs page."""

    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    match_host: str = Field(default="", max_length=253)
    # null / omit clear → remove this exact rule
    country_code: str | None = Field(default=None, max_length=2)


class ConfigLocationProbeItem(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    match_host: str = Field(default="", max_length=253)
    source_remark: str | None = Field(default=None, max_length=500)
    label: str | None = Field(default=None, max_length=255)
    country_code: str | None = Field(default=None, max_length=2)


class ConfigLocationProbeIn(BaseModel):
    """Probe one or many endpoints → set country flags (overwrites by default)."""

    items: list[ConfigLocationProbeItem] = Field(default_factory=list, max_length=5000)
    overwrite: bool = True


class ConfigLocationProbeResult(BaseModel):
    host: str
    port: int
    match_host: str = ""
    country_code: str | None = None
    flag: str | None = None
    source: str = "none"  # remark | geo | failed | skipped
    ok: bool = False
    detail: str | None = None


class ConfigLocationProbeOut(BaseModel):
    ok: bool = True
    updated: int = 0
    resolved: int = 0
    failed: int = 0
    total: int = 0
    results: list[ConfigLocationProbeResult] = Field(default_factory=list)


class ConfigLocationOut(BaseModel):
    id: int
    host: str
    port: int
    match_host: str = ""
    country_code: str
    flag: str
    label: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}
