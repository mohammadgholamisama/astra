from pydantic import BaseModel, Field


class SiteSettingsOut(BaseModel):
    sub_base_url: str
    panel_base_url: str = ""
    brand_name: str
    link_channel: str = ""
    link_support: str = ""
    link_app: str = ""
    config_brand_tag: str = ""
    promo_config_enabled: bool = False
    promo_config_text: str = ""
    promo_config_text_2: str = ""
    status_line_template: str = "✨ {username} - {remain} - {expire} ✨"
    status_panel_label: str = "وضعیت"
    ssl_available: bool = False
    panel_domain: str = ""
    sub_domain: str = ""
    defaults: dict[str, str] = Field(default_factory=dict)


class SiteSettingsUpdate(BaseModel):
    sub_base_url: str | None = Field(default=None, max_length=512)
    panel_base_url: str | None = Field(default=None, max_length=512)
    brand_name: str | None = Field(default=None, max_length=48)
    link_channel: str | None = Field(default=None, max_length=512)
    link_support: str | None = Field(default=None, max_length=512)
    link_app: str | None = Field(default=None, max_length=512)
    config_brand_tag: str | None = Field(default=None, max_length=24)
    promo_config_enabled: bool | None = None
    promo_config_text: str | None = Field(default=None, max_length=200)
    promo_config_text_2: str | None = Field(default=None, max_length=200)
    status_line_template: str | None = Field(default=None, max_length=200)
    status_panel_label: str | None = Field(default=None, max_length=48)
    reset_sub_base_url: bool = False
    reset_panel_base_url: bool = False
    reset_brand_name: bool = False
    clear_link_channel: bool = False
    clear_link_support: bool = False
    clear_link_app: bool = False
    clear_config_brand_tag: bool = False
    clear_promo_config: bool = False
    reset_status_line: bool = False


class SslIssueRequest(BaseModel):
    target: str = Field(description="panel or sub")
    domain: str = Field(min_length=3, max_length=253)


class SslIssueResponse(BaseModel):
    ok: bool
    message: str
    domain: str = ""
    target: str = ""
