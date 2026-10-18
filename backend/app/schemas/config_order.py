from pydantic import BaseModel, Field


class ConfigOrderItemIn(BaseModel):
    host: str
    port: int = Field(..., ge=1, le=65535)
    match_host: str = ""
    label: str | None = None


class ConfigOrderItemOut(BaseModel):
    host: str
    port: int
    match_host: str = ""
    label: str | None = None
    sort_order: int = 0
    in_sample: bool = True
    sample_link: str | None = None
    protocol: str | None = None
    panel_names: list[str] = Field(default_factory=list)
    group_names: list[str] = Field(default_factory=list)
    hidden: bool = False
    country_code: str | None = None
    flag: str | None = None
    flag_exact: bool = False


class ConfigOrderWorkspaceOut(BaseModel):
    items: list[ConfigOrderItemOut]
    sample_ready: bool = False
    total_sample_links: int = 0
    ordered_count: int = 0


class ConfigOrderSaveBody(BaseModel):
    items: list[ConfigOrderItemIn]
