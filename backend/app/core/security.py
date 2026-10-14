from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import JWTError, jwt

from app.core.config import get_settings

settings = get_settings()


def hash_password(password: str) -> str:
    """Hash with bcrypt directly (avoids passlib/bcrypt version traps)."""
    raw = (password or "").encode("utf-8")
    # bcrypt only uses first 72 bytes
    if len(raw) > 72:
        raw = raw[:72]
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        raw = (plain or "").encode("utf-8")
        if len(raw) > 72:
            raw = raw[:72]
        h = (hashed or "").encode("ascii", errors="ignore")
        if not h:
            return False
        return bcrypt.checkpw(raw, h)
    except Exception:  # noqa: BLE001
        return False


def create_access_token(subject: str, extra: dict[str, Any] | None = None) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload: dict[str, Any] = {"sub": subject, "exp": expire}
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_token(token: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        return None
