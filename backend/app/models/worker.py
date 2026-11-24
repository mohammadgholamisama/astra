"""Remote Astra worker nodes — create / manage / usage offload."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.types import JSONType


class WorkerNode(Base):
    """A remote server that runs panel ops for assigned panels.

    Master routes create/modify/delete/get_usage through the worker when online;
    on timeout/unreachable it falls back to local adapters on this host.
    """

    __tablename__ = "worker_nodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    base_url: Mapped[str] = mapped_column(String(512))
    # sha256 hex of bearer token (raw shown once on create/rotate)
    token_hash: Mapped[str] = mapped_column(String(64), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # online | offline | unknown
    status: Mapped[str] = mapped_column(String(16), default="unknown", index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional SSH hint for install helper (host/user only — never store password)
    ssh_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ssh_user: Mapped[str | None] = mapped_column(String(64), nullable=True)
    listen_port: Mapped[int] = mapped_column(Integer, default=2090)
    meta: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    panels = relationship("Panel", back_populates="worker")


# Late import style: Panel.worker_id added on Panel model.
