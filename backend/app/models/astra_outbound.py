"""Astra panel outbound pool — share-link upstreams behind the local node."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
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


class AstraOutbound(Base):
    __tablename__ = "astra_outbounds"
    __table_args__ = (
        UniqueConstraint("panel_id", "uri_hash", name="uq_astra_outbound_panel_uri"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    panel_id: Mapped[int] = mapped_column(
        ForeignKey("panels.id", ondelete="CASCADE"), index=True
    )
    uri: Mapped[str] = mapped_column(Text)
    uri_hash: Mapped[str] = mapped_column(String(64), index=True)
    remark: Mapped[str] = mapped_column(String(255), default="")
    protocol: Mapped[str] = mapped_column(String(32), default="vless")
    host: Mapped[str] = mapped_column(String(255), default="")
    port: Mapped[int] = mapped_column(Integer, default=0)
    parsed: Mapped[dict] = mapped_column(JSONType, default=dict)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_ping_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_ping_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_ping_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    panel = relationship("Panel", back_populates="astra_outbounds")


class AstraNodeClient(Base):
    """Per-user identity on the local Astra node (UUID + quota + usage)."""

    __tablename__ = "astra_node_clients"
    __table_args__ = (
        UniqueConstraint("panel_id", "email", name="uq_astra_node_panel_email"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    panel_id: Mapped[int] = mapped_column(
        ForeignKey("panels.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(128), index=True)
    uuid: Mapped[str] = mapped_column(String(64), index=True)
    enable: Mapped[bool] = mapped_column(Boolean, default=True)
    data_limit: Mapped[int] = mapped_column(BigInteger, default=0)
    expire: Mapped[int] = mapped_column(BigInteger, default=0)
    limit_ip: Mapped[int] = mapped_column(Integer, default=0)
    used_up: Mapped[int] = mapped_column(BigInteger, default=0)
    used_down: Mapped[int] = mapped_column(BigInteger, default=0)
    # Last Xray StatsService counter (uplink+downlink). NULL = not primed yet.
    # used_down/up are cumulative; this field tracks the live counter for deltas
    # so traffic survives Xray restarts without double-counting or stalls.
    stats_counter: Mapped[int | None] = mapped_column(BigInteger, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    panel = relationship("Panel", back_populates="astra_node_clients")
