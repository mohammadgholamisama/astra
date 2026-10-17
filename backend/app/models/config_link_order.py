"""Global subscription link order: host:port[+header host] → sort position."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ConfigLinkOrder(Base):
    """Admin-defined global order for share links in every subscription feed.

    Matching key is ``(host, port, match_host)`` — same fingerprint as hide/location.
    Only links the user actually has appear; missing panels simply skip their rows.
    """

    __tablename__ = "config_link_orders"
    __table_args__ = (
        UniqueConstraint(
            "host",
            "port",
            "match_host",
            name="uq_config_link_order_host_port_match",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    host: Mapped[str] = mapped_column(String(253), index=True)
    port: Mapped[int] = mapped_column(Integer)
    match_host: Mapped[str] = mapped_column(
        String(253), default="", server_default=text("''")
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, index=True)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
