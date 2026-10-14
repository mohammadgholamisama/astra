from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    note: str | None = None
    expires_at: datetime | None = None


class ApiKeyOut(ORMModel):
    id: int
    name: str
    key_prefix: str
    note: str | None
    is_active: bool
    expires_at: datetime | None
    created_at: datetime


class ApiKeyCreated(ApiKeyOut):
    key: str  # shown once
