"""Optional Redis helpers — shared sub-feed cache across uvicorn workers.

Falls back silently when Redis is down so /sub never depends on it.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("astra.redis")

_CLIENT: Any = None
_TRIED = False


def redis_url() -> str:
    return (
        (os.environ.get("ASTRA_REDIS_URL") or "").strip()
        or (os.environ.get("REDIS_URL") or "").strip()
        or "redis://127.0.0.1:6379/0"
    )


def get_redis():
    """Lazy sync client (feed cache is sync on the hot /sub path)."""
    global _CLIENT, _TRIED
    if _CLIENT is not None:
        return _CLIENT
    if _TRIED:
        return None
    _TRIED = True
    try:
        import redis  # type: ignore

        client = redis.Redis.from_url(
            redis_url(),
            socket_connect_timeout=0.4,
            socket_timeout=0.6,
            decode_responses=False,
            health_check_interval=30,
        )
        client.ping()
        _CLIENT = client
        logger.info("redis connected url=%s", redis_url().split("@")[-1])
        return _CLIENT
    except Exception as exc:  # noqa: BLE001
        logger.warning("redis unavailable — in-process cache only: %s", exc)
        _CLIENT = None
        return None


def sub_ttl_seconds() -> int:
    try:
        return max(15, int(os.environ.get("ASTRA_SUB_REDIS_TTL", "45") or 45))
    except (TypeError, ValueError):
        return 45


def sub_key(token: str, op_bucket: str = "global") -> str:
    tok = (token or "").strip()
    return f"astra:sub:{tok}:{op_bucket or 'global'}"


def sub_cache_get(token: str, op_bucket: str = "global") -> bytes | None:
    client = get_redis()
    if client is None:
        return None
    try:
        raw = client.get(sub_key(token, op_bucket))
        if isinstance(raw, (bytes, bytearray)) and raw:
            return bytes(raw)
    except Exception:  # noqa: BLE001
        pass
    return None


def _uid_key(user_id: int) -> str:
    return f"astra:subuid:{int(user_id)}"


def sub_cache_set(
    token: str,
    body: bytes,
    *,
    op_bucket: str = "global",
    ttl: int | None = None,
    user_id: int | None = None,
) -> None:
    if not body:
        return
    client = get_redis()
    if client is None:
        return
    try:
        exp = int(ttl or sub_ttl_seconds())
        pipe = client.pipeline(transaction=False)
        pipe.setex(sub_key(token, op_bucket), exp, body)
        if user_id is not None:
            uk = _uid_key(int(user_id))
            pipe.sadd(uk, (token or "").strip().encode("utf-8", "ignore"))
            pipe.expire(uk, max(exp * 4, 300))
        pipe.execute()
    except Exception:  # noqa: BLE001
        pass


def sub_cache_delete_token(token: str) -> None:
    """Drop all op-bucket variants for a token (pattern delete capped)."""
    client = get_redis()
    if client is None:
        return
    tok = (token or "").strip()
    if not tok:
        return
    try:
        keys = [sub_key(tok, op) for op in ("global", "mci", "irancell", "rightel", "other")]
        client.delete(*keys)
    except Exception:  # noqa: BLE001
        pass


def sub_cache_invalidate_users(*user_ids: int) -> None:
    client = get_redis()
    if client is None:
        return
    want = [int(u) for u in user_ids if u is not None]
    if not want:
        return
    try:
        for uid in want:
            uk = _uid_key(uid)
            members = client.smembers(uk) or set()
            keys: list[str] = [uk]
            for raw in members:
                tok = (
                    raw.decode("utf-8", "ignore")
                    if isinstance(raw, (bytes, bytearray))
                    else str(raw)
                )
                if tok:
                    keys.extend(
                        sub_key(tok, op)
                        for op in ("global", "mci", "irancell", "rightel", "other")
                    )
            if keys:
                client.delete(*keys)
    except Exception:  # noqa: BLE001
        pass
