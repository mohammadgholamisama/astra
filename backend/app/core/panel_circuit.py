"""Per-panel circuit breaker — skip remotes that just timed out.

Opens after 2 consecutive timeouts (or one hard trip). Shared via Redis when
available so all uvicorn workers honor the same open state.
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger("astra.circuit")

_LOCAL_OPEN: dict[int, float] = {}
_STRIKES: dict[int, int] = {}
_DEFAULT_TTL = float(os.environ.get("ASTRA_PANEL_CIRCUIT_SECONDS", "30") or 30)
_STRIKE_NEED = max(1, int(os.environ.get("ASTRA_PANEL_CIRCUIT_STRIKES", "2") or 2))


def _ttl() -> float:
    return max(10.0, float(_DEFAULT_TTL))


def note_panel_timeout(panel_id: int, *, reason: str = "") -> bool:
    """Record a timeout. Returns True if the circuit just opened."""
    pid = int(panel_id)
    if pid <= 0:
        return False
    if panel_circuit_open(pid):
        return False
    n = int(_STRIKES.get(pid, 0) or 0) + 1
    _STRIKES[pid] = n
    if n < _STRIKE_NEED:
        logger.debug(
            "circuit strike panel=%s n=%s/%s reason=%s",
            pid,
            n,
            _STRIKE_NEED,
            (reason or "")[:80],
        )
        return False
    trip_panel(pid, reason=reason or "timeout x2")
    _STRIKES.pop(pid, None)
    return True


def trip_panel(panel_id: int, *, seconds: float | None = None, reason: str = "") -> None:
    pid = int(panel_id)
    if pid <= 0:
        return
    sec = float(seconds) if seconds is not None else _ttl()
    until = time.monotonic() + sec
    _LOCAL_OPEN[pid] = until
    _STRIKES.pop(pid, None)
    try:
        from app.core.redis_cache import get_redis

        client = get_redis()
        if client is not None:
            client.setex(f"astra:panel:down:{pid}", int(sec), b"1")
    except Exception:  # noqa: BLE001
        pass
    logger.info(
        "circuit OPEN panel=%s sec=%.0f reason=%s",
        pid,
        sec,
        (reason or "")[:120],
    )


def panel_circuit_open(panel_id: int) -> bool:
    """True when the panel should be skipped (recently timed out / failing)."""
    pid = int(panel_id)
    now = time.monotonic()
    local = float(_LOCAL_OPEN.get(pid, 0.0) or 0.0)
    if local > now:
        return True
    if local and local <= now:
        _LOCAL_OPEN.pop(pid, None)
    try:
        from app.core.redis_cache import get_redis

        client = get_redis()
        if client is not None and client.exists(f"astra:panel:down:{pid}"):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def clear_panel_circuit(panel_id: int) -> None:
    pid = int(panel_id)
    _LOCAL_OPEN.pop(pid, None)
    _STRIKES.pop(pid, None)
    try:
        from app.core.redis_cache import get_redis

        client = get_redis()
        if client is not None:
            client.delete(f"astra:panel:down:{pid}")
    except Exception:  # noqa: BLE001
        pass


def note_panel_ok(panel_id: int) -> None:
    """Clear strikes after a successful remote call (does not force-close OPEN)."""
    _STRIKES.pop(int(panel_id), None)
