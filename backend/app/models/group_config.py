"""Per-config rules for a sales group (coefficient for sub + IP limit for remotes)."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class GroupConfigItem(Base):
    """
    One row ≈ one outbound config type produced by panels in a group.
    - coefficient: applied later when building Astra subscription (not on remotes)
    - ip_limit*: pushed to the remote panel for that panel's user
    """

    __tablename__ = "group_config_items"
    __table_args__ = (
        UniqueConstraint("group_id", "config_key", name="uq_group_config_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    panel_id: Mapped[int] = mapped_column(ForeignKey("panels.id", ondelete="CASCADE"), index=True)
    config_key: Mapped[str] = mapped_column(String(255))
    label: Mapped[str] = mapped_column(String(255), default="")
    protocol: Mapped[str] = mapped_column(String(64), default="")
    sample_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    coefficient: Mapped[float] = mapped_column(Float, default=1.0)
    ip_limit_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    ip_limit: Mapped[int] = mapped_column(Integer, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    group = relationship("Group", backref="config_items")
    panel = relationship("Panel")
