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


class ConfigVisibilityOut(BaseModel):
    group_id: int
    group_name: str
    sample_ready: bool = False
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
