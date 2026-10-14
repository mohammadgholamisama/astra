from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.types import IntListType, JSONType


class UserTemplate(Base):
    __tablename__ = "user_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    group_ids: Mapped[list[int]] = mapped_column(IntListType, default=list)
    data_limit: Mapped[int] = mapped_column(BigInteger, default=0)
    expire_duration: Mapped[int] = mapped_column(Integer, default=0)
    username_prefix: Mapped[str | None] = mapped_column(String(20), nullable=True)
    username_suffix: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    data_limit_reset_strategy: Mapped[str] = mapped_column(String(32), default="no_reset")
    is_disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    extra_settings: Mapped[dict] = mapped_column(JSONType, default=dict)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
