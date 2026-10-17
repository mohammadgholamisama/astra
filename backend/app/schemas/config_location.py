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
