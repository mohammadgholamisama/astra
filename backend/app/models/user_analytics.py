"""Per-user analytics: raw hourly usage, online sessions, sub-client sightings."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class UserUsageState(Base):
    """Cursor + lifetime peaks for one Astra user (raw bytes, no coefficient)."""

    __tablename__ = "user_usage_states"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # Last observed Σ PanelAccount.used_traffic (raw)
    last_raw_total: Mapped[int] = mapped_column(BigInteger, default=0)
    last_sample_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Contiguous online window start (UTC); None = currently offline
    online_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    online_raw_at_start: Mapped[int] = mapped_column(BigInteger, default=0)
    # Consecutive low-delta samples while "online" (end session after 2)
    idle_streak: Mapped[int] = mapped_column(Integer, default=0)
    # Lifetime peak instantaneous rate (bits per second) while sampling
    peak_bps: Mapped[float] = mapped_column(Float, default=0.0)
    peak_bps_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_sub_fetch_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UsageHourBucket(Base):
    """Raw (no-coeff) bytes consumed by a user in one UTC hour."""

    __tablename__ = "usage_hour_buckets"
    __table_args__ = (
        UniqueConstraint("user_id", "hour_ts", name="uq_usage_hour_user"),
        Index("ix_usage_hour_ts", "hour_ts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Unix timestamp aligned to hour start (UTC)
    hour_ts: Mapped[int] = mapped_column(BigInteger, index=True)
    raw_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    # Approximate seconds with traffic in this hour
    active_seconds: Mapped[int] = mapped_column(Integer, default=0)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    peak_bps: Mapped[float] = mapped_column(Float, default=0.0)


class UsageSession(Base):
    """One online window detected from raw traffic deltas."""

    __tablename__ = "usage_sessions"
    __table_args__ = (Index("ix_usage_session_user_start", "user_id", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    raw_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    peak_bps: Mapped[float] = mapped_column(Float, default=0.0)
    avg_bps: Mapped[float] = mapped_column(Float, default=0.0)
    open: Mapped[int] = mapped_column(Integer, default=1)  # 1=open, 0=closed


class SubClientProfile(Base):
    """Distinct device / network fingerprints seen on subscription fetches."""

    __tablename__ = "sub_client_profiles"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "key", name="uq_sub_client_profile"),
        Index("ix_sub_client_user_kind", "user_id", "kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # "device" | "network"
    kind: Mapped[str] = mapped_column(String(16))
    key: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(200), default="")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    hit_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SubFetchEvent(Base):
    """Recent subscription update requests (retention-pruned)."""

    __tablename__ = "sub_fetch_events"
    __table_args__ = (Index("ix_sub_fetch_user_ts", "user_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    client_ip: Mapped[str] = mapped_column(String(64), default="")
    network_key: Mapped[str] = mapped_column(String(64), default="")
    device_key: Mapped[str] = mapped_column(String(64), default="")
    client_app: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(400), default="")
    country: Mapped[str | None] = mapped_column(String(8), nullable=True)
