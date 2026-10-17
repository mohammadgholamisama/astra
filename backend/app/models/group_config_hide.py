"""Global hide rules: host:port[+header host] excluded from ALL subscription feeds."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class GroupConfigHide(Base):
    """Presence of a row means this endpoint is hidden from every user's sub.

    Matching key is ``(host, port, match_host)`` — independent of sales group.
    ``match_host`` is optional link header/SNI (host= / sni=). Empty = no header.
    ``group_id`` is optional audit/source only (nullable); hide itself is global.
    """

    __tablename__ = "group_config_hides"
    __table_args__ = (
        UniqueConstraint(
            "host",
            "port",
            "match_host",
            name="uq_config_hide_host_port_match",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Optional: which group the admin was viewing when they hid it (audit only)
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    host: Mapped[str] = mapped_column(String(253), index=True)
    port: Mapped[int] = mapped_column(Integer)
    match_host: Mapped[str] = mapped_column(
        String(253), default="", server_default=text("''")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
