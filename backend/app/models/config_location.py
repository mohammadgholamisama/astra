"""Map share-link host:port[+header host] → country flag for subscription remarks."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ConfigLocation(Base):
    __tablename__ = "config_locations"
    __table_args__ = (
        UniqueConstraint(
            "host",
            "port",
            "match_host",
            name="uq_config_location_host_port_match",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Normalized lowercase server hostname/IP from @host:port
    host: Mapped[str] = mapped_column(String(253), index=True)
    port: Mapped[int] = mapped_column(Integer)
    # Optional: link query host/sni (e.g. host=turkiye.varzeshfotsal.ir). "" = any.
    match_host: Mapped[str] = mapped_column(
        String(253), default="", server_default=text("''")
    )
    # ISO 3166-1 alpha-2 (e.g. DE) — flag emoji derived at runtime
    country_code: Mapped[str] = mapped_column(String(2))
    # Optional admin label (e.g. «آلمان»)
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
