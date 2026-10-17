from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.types import JSONType


class GroupCategory(Base):
    """Admin folder/tab for organizing sales groups on the Groups page."""

    __tablename__ = "group_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    groups = relationship("Group", back_populates="category")


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Optional short tag in real config remarks: ``#1. TAG 2.5G | name`` (empty = omitted)
    config_tag: Mapped[str | None] = mapped_column(String(24), nullable=True, default="")
    # Optional info messages → fake configs prepended to subscription (first in list)
    notices: Mapped[list | None] = mapped_column(JSONType, nullable=True, default=list)
    # NULL = default bucket «همه گروه‌ها» on the Groups page
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("group_categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    category = relationship("GroupCategory", back_populates="groups")
    panel_links = relationship("GroupPanel", back_populates="group", cascade="all, delete-orphan")
    users = relationship("User", secondary="user_groups", back_populates="groups")


class GroupPanel(Base):
    __tablename__ = "group_panels"
    __table_args__ = (UniqueConstraint("group_id", "panel_id", name="uq_group_panel"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    panel_id: Mapped[int] = mapped_column(ForeignKey("panels.id", ondelete="CASCADE"), index=True)
    config: Mapped[dict] = mapped_column(JSONType, default=dict)
    priority: Mapped[int] = mapped_column(Integer, default=0)

    group = relationship("Group", back_populates="panel_links")
    panel = relationship("Panel", back_populates="group_links")


class UserGroup(Base):
    __tablename__ = "user_groups"
    __table_args__ = (UniqueConstraint("user_id", "group_id", name="uq_user_group"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    # Selection order on create/edit → subscription config order
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
