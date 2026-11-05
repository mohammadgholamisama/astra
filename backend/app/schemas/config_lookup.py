from pydantic import BaseModel, Field


class ConfigLookupIn(BaseModel):
    """Paste a share link and/or an Astra subscription remark / config_tag."""

    text: str = Field(..., min_length=1, max_length=50_000)


class ConfigLookupHit(BaseModel):
    panel_id: int
    panel_name: str
    panel_type: str | None = None
    panel_is_active: bool = True
    group_id: int | None = None
    group_name: str | None = None
    group_config_tag: str | None = None
    order_kind: str | None = None
    source_remark: str | None = None
    match_quality: str = "exact"  # tag | exact | sni | host_port | uuid | uri
    host: str = ""
    port: int = 0
    match_host: str = ""
    accounts_count: int = 0
    sample_users: list[str] = Field(default_factory=list)


class ConfigLookupParsed(BaseModel):
    link_preview: str = ""
    protocol: str | None = None
    remark: str | None = None
    config_tag: str | None = None
    host: str = ""
    port: int = 0
    match_host: str = ""


class ConfigLookupOut(BaseModel):
    parsed: ConfigLookupParsed | None = None
    hits: list[ConfigLookupHit] = Field(default_factory=list)
    links_found: int = 0
    detail: str | None = None
