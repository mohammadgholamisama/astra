import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.types import JSONType


class PanelType(str, enum.Enum):
    sanaei = "sanaei"
    pasarguard = "pasarguard"
    marzban = "marzban"
    rebecca = "rebecca"
    guard = "guard"
    astra = "astra"


class PanelCategory(Base):
    """Admin folder/tab for organizing panels on the Panels page only.

    Does not affect provision, subscription, or group behavior.
    """

    __tablename__ = "panel_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    panels = relationship("Panel", back_populates="category")


class Panel(Base):
    __tablename__ = "panels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    type: Mapped[PanelType] = mapped_column(
        Enum(PanelType, name="panel_type", native_enum=False), index=True
    )
    base_url: Mapped[str] = mapped_column(String(512))
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    api_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict] = mapped_column(JSONType, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # NULL = default bucket «همه پنل‌ها» on the Panels page (UI only)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("panel_categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        default=None,
    )
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    category = relationship("PanelCategory", back_populates="panels")
    group_links = relationship("GroupPanel", back_populates="panel", cascade="all, delete-orphan")
    accounts = relationship("PanelAccount", back_populates="panel", cascade="all, delete-orphan")
    astra_outbounds = relationship(
        "AstraOutbound", back_populates="panel", cascade="all, delete-orphan"
    )
    astra_node_clients = relationship(
        "AstraNodeClient", back_populates="panel", cascade="all, delete-orphan"
    )
