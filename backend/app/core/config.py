from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Prefer project-root .env, then backend/.env
_ROOT = Path(__file__).resolve().parents[3]
_BACKEND = Path(__file__).resolve().parents[2]
_ENV_FILES = tuple(
    str(p)
    for p in (_ROOT / ".env", _BACKEND / ".env")
    if p.exists()
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILES or ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: str = "sqlite+aiosqlite:///./astra.db"
    # Postgres pool — sized so analytics/UI never starve behind usage pulls.
    DB_POOL_SIZE: int = 48
    DB_MAX_OVERFLOW: int = 36
    DB_POOL_TIMEOUT: float = 8.0
    DB_POOL_RECYCLE: int = 300
    SECRET_KEY: str = "astra-dev-secret-key-change-in-production-32chars"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
    SUB_BASE_URL: str = "http://localhost:8000"
    PANEL_BASE_URL: str = "http://localhost:5173"
    PANEL_NAME: str = "Astra"
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin123"
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173"
    ALGORITHM: str = "HS256"
    ADAPTER_TIMEOUT: float = 35.0
    # Max concurrent HTTP ops to remotes (create/delete/modify). Caps FD/TLS storm
    # when a user spans many panels; wall time still ≈ ceil(N/limit)×slowest.
    PANEL_REMOTE_CONCURRENCY: int = 20
    # Background quota watch cadence (seconds). Exhausted users are frozen
    # on every remote in the same cycle.
    USAGE_REFRESH_SECONDS: int = 5
    # Full-fleet usage pull (all panels + dirty recount), seconds.
    # Target: every ~2 minutes all remotes are re-queried in parallel.
    USAGE_FLEET_SECONDS: int = 45
    # Hot loop for /sub-active users (seconds).
    USAGE_HOT_SECONDS: int = 4
    # Official install helper for SSL / nginx (empty = SSL UI disabled)
    ASTRA_HELPER: str = ""
    ASTRA_APP_PORT: int = 8000
    ASTRA_PANEL_DOMAIN: str = ""
    ASTRA_SUB_DOMAIN: str = ""
    FRONTEND_DIST: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
