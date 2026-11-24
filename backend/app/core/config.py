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
    # Postgres pool — keep small so uvicorn cannot open 70+ backends and
    # starve SSH/CPU. Interactive API keeps headroom via background_db_sem.
    # Slightly larger pool so interactive API keeps slots while a few bg
    # remotes run — still capped; leaks are fixed by closing sessions on HTTP.
    # Under Postgres max_connections=100. Too small → QueuePool timeout on
    # boot; too large → «too many clients» and used_traffic stuck at 0.
    DB_POOL_SIZE: int = 16
    DB_MAX_OVERFLOW: int = 6
    DB_POOL_TIMEOUT: float = 5.0
    DB_POOL_RECYCLE: int = 120
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
    PANEL_REMOTE_CONCURRENCY: int = 4
    # Background quota watch cadence (seconds). Exhausted users are frozen
    # on every remote in the same cycle. Accuracy via fleet bulk + hot /sub.
    USAGE_REFRESH_SECONDS: int = 120
    # Fleet rotate / full bulk cadence (seconds). Worker collect owns assigned
    # panels every 2 min; master fleet covers unassigned + worker-down failover.
    USAGE_FLEET_SECONDS: int = 120
    # Hot loop for recent /sub users (seconds).
    USAGE_HOT_SECONDS: int = 60
    # Auto «رفع خطا» on Users page — off by default; use the button manually.
    AUTO_RETRY_ERRORS: bool = False
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
