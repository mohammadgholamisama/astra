"""Per-panel analytics: raw hourly usage, online users, ping-test quality."""

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
from app.core.types import JSONType


class PanelUsageState(Base):
    """Cursor for one panel's raw traffic + lifetime peaks."""

    __tablename__ = "panel_usage_states"

    panel_id: Mapped[int] = mapped_column(
        ForeignKey("panels.id", ondelete="CASCADE"), primary_key=True
    )
    last_raw_total: Mapped[int] = mapped_column(BigInteger, default=0)
    last_sample_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Live watermarks for accurate local hour/day (bytes = live - mark_raw)
    period_hour_ts: Mapped[int] = mapped_column(BigInteger, default=0)
    period_hour_raw: Mapped[int] = mapped_column(BigInteger, default=0)
    period_day_ts: Mapped[int] = mapped_column(BigInteger, default=0)
    period_day_raw: Mapped[int] = mapped_column(BigInteger, default=0)
    peak_bps: Mapped[float] = mapped_column(Float, default=0.0)
    peak_bps_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Lifetime counters from ping-rank syncs
    tests_total: Mapped[int] = mapped_column(Integer, default=0)
    configs_tested_total: Mapped[int] = mapped_column(Integer, default=0)
    top100_total: Mapped[int] = mapped_column(Integer, default=0)
    good_total: Mapped[int] = mapped_column(Integer, default=0)
    all_ops_ok_total: Mapped[int] = mapped_column(Integer, default=0)
    last_test_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PanelUsageHourBucket(Base):
    """Raw bytes for one panel in one UTC hour."""

    __tablename__ = "panel_usage_hour_buckets"
    __table_args__ = (
        UniqueConstraint("panel_id", "hour_ts", name="uq_panel_usage_hour"),
        Index("ix_panel_usage_hour_ts", "hour_ts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    panel_id: Mapped[int] = mapped_column(
        ForeignKey("panels.id", ondelete="CASCADE"), index=True
    )
    hour_ts: Mapped[int] = mapped_column(BigInteger, index=True)
    raw_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    active_seconds: Mapped[int] = mapped_column(Integer, default=0)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    peak_bps: Mapped[float] = mapped_column(Float, default=0.0)
    # Sum of online_user counts across samples (avg = online_sum / sample_count)
    online_sum: Mapped[int] = mapped_column(Integer, default=0)
    online_peak: Mapped[int] = mapped_column(Integer, default=0)


class PanelUserTrafficState(Base):
    """Per (user × panel) raw cursor for online detection on that panel."""

    __tablename__ = "panel_user_traffic_states"
    __table_args__ = (
        UniqueConstraint("user_id", "panel_id", name="uq_panel_user_traffic"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    panel_id: Mapped[int] = mapped_column(
        ForeignKey("panels.id", ondelete="CASCADE"), index=True
    )
    last_raw: Mapped[int] = mapped_column(BigInteger, default=0)
    last_sample_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    online_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    idle_streak: Mapped[int] = mapped_column(Integer, default=0)


class PanelTestRun(Base):
    """One ping-rank sync snapshot broken down per panel."""

    __tablename__ = "panel_test_runs"
    __table_args__ = (Index("ix_panel_test_run_panel_ts", "panel_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    panel_id: Mapped[int] = mapped_column(
        ForeignKey("panels.id", ondelete="CASCADE"), index=True
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    tested: Mapped[int] = mapped_column(Integer, default=0)
    in_top100: Mapped[int] = mapped_column(Integer, default=0)
    good: Mapped[int] = mapped_column(Integer, default=0)
    all_ops_ok: Mapped[int] = mapped_column(Integer, default=0)
    avg_ping: Mapped[float] = mapped_column(Float, default=-1.0)
    detail: Mapped[dict | None] = mapped_column(JSONType, nullable=True, default=dict)
