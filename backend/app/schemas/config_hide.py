from pydantic import BaseModel, Field


class ConfigVisibilityItemOut(BaseModel):
    host: str
    port: int
    match_host: str = ""
    label: str = ""
    protocol: str = ""
    count: int = 0
    panel_names: list[str] = Field(default_factory=list)
    hidden: bool = False
    orphan: bool = False
    # Present in «all panels» aggregate view
    group_id: int | None = None
    group_name: str | None = None
    # Global flag for this host:port[+match] (same key as sub remarks)
    country_code: str | None = None
    flag: str | None = None
    # True = DB rule is for this exact fingerprint (clear is safe)
    flag_exact: bool = False


class ConfigVisibilityOut(BaseModel):
    group_id: int
    group_name: str
    sample_ready: bool = False
    sample_username: str | None = None
    items: list[ConfigVisibilityItemOut] = Field(default_factory=list)
    hidden_count: int = 0
    total_count: int = 0


class ConfigVisibilityAllOut(BaseModel):
    """Aggregate visibility across every active sales group / panel."""

    items: list[ConfigVisibilityItemOut] = Field(default_factory=list)
    hidden_count: int = 0
    total_count: int = 0
    groups_count: int = 0
    sample_ready_groups: int = 0


class ConfigVisibilitySet(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    match_host: str = Field(default="", max_length=253)
    hidden: bool = True


class ConfigVisibilityBulkSet(BaseModel):
    items: list[ConfigVisibilitySet] = Field(default_factory=list, max_length=500)


class SampleAllGroupResult(BaseModel):
    group_id: int
    group_name: str
    ok: bool = False
    sample_username: str | None = None
    items_count: int = 0
    error: str | None = None


class SampleAllOut(BaseModel):
    """Result of creating/refreshing probe users on every active group."""

    ok_count: int = 0
    fail_count: int = 0
    groups: list[SampleAllGroupResult] = Field(default_factory=list)
    visibility: ConfigVisibilityAllOut | None = None
