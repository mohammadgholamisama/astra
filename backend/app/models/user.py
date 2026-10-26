import enum
import secrets
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.types import JSONType


class UserStatus(str, enum.Enum):
    active = "active"
    disabled = "disabled"
    limited = "limited"
    expired = "expired"
    on_hold = "on_hold"


class PanelAccountStatus(str, enum.Enum):
    pending = "pending"
    active = "active"
    error = "error"
    deleted = "deleted"


def generate_sub_token() -> str:
    return secrets.token_urlsafe(24)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name="user_status", native_enum=False),
        default=UserStatus.active,
        index=True,
    )
    data_limit: Mapped[int] = mapped_column(BigInteger, default=0)
    used_traffic: Mapped[int] = mapped_column(BigInteger, default=0)
    lifetime_used_traffic: Mapped[int] = mapped_column(BigInteger, default=0)
    expire: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Seconds of validity after first use. Used while status=on_hold (expire=0).
    on_hold_expire_duration: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    sub_token: Mapped[str] = mapped_column(String(64), unique=True, index=True, default=generate_sub_token)
    data_limit_reset_strategy: Mapped[str] = mapped_column(String(32), default="no_reset")
    proxy_settings: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    groups = relationship(
        "Group",
        secondary="user_groups",
        back_populates="users",
        order_by="UserGroup.sort_order, UserGroup.id",
    )
    panel_accounts = relationship("PanelAccount", back_populates="user", cascade="all, delete-orphan")


class PanelAccount(Base):
    __tablename__ = "panel_accounts"
    # One remote user per (Astra user × panel × sales group) so coefficients
    # and tags stay accurate when several groups share the same panel.
    __table_args__ = (
        UniqueConstraint("user_id", "panel_id", "group_id", name="uq_user_panel_group"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    panel_id: Mapped[int] = mapped_column(ForeignKey("panels.id", ondelete="CASCADE"), index=True)
    # Astra sales group this remote account belongs to (required for multi-group panels).
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    remote_username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    remote_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[PanelAccountStatus] = mapped_column(
        Enum(PanelAccountStatus, name="panel_account_status", native_enum=False),
        default=PanelAccountStatus.pending,
    )
    links: Mapped[list] = mapped_column(JSONType, default=list)
    # Optional: astra group_id (str) → share links belonging to that group on this panel.
    # Used when several groups share one panel (PasarGuard remote_group_ids / Marzban protocols).
    links_by_group: Mapped[dict | None] = mapped_column(JSONType, nullable=True, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    used_traffic: Mapped[int] = mapped_column(BigInteger, default=0)
    # Coefficient periods: past usage stays at old rate; only delta uses current coeff.
    # weighted = coeff_anchor_weighted + (raw - coeff_anchor_raw) * applied_coefficient
    coeff_anchor_raw: Mapped[int] = mapped_column(BigInteger, default=0)
    coeff_anchor_weighted: Mapped[int] = mapped_column(BigInteger, default=0)
    applied_coefficient: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="panel_accounts")
    panel = relationship("Panel", back_populates="accounts")
