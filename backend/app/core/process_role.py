"""Multi-process role helpers for uvicorn --workers.

Only one process should run fleet/quota/hot background loops. API workers
still bootstrap worker routes and the usage-report queue so create/delete/
usage stay fast and spread across cores.
"""

from __future__ import annotations

import atexit
import fcntl
import logging
import os
from pathlib import Path

logger = logging.getLogger("astra.process")

_LOCK_FD = None
_IS_BG: bool | None = None


def bg_lock_path() -> Path:
    raw = (os.environ.get("ASTRA_BG_LOCK") or "").strip()
    if raw:
        return Path(raw)
    return Path("/tmp/astra-bg.lock")


def acquire_background_role() -> bool:
    """Try to become the single background-loop process (non-blocking)."""
    global _LOCK_FD, _IS_BG
    if _IS_BG is not None:
        return _IS_BG
    # Explicit override for dedicated bg units
    role = (os.environ.get("ASTRA_ROLE") or "").strip().lower()
    if role in ("api", "web"):
        _IS_BG = False
        return False
    if role in ("bg", "background", "all"):
        if role != "all":
            _IS_BG = True
            return True
    path = bg_lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(path, "w", encoding="utf-8")
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(str(os.getpid()))
        fd.flush()
        _LOCK_FD = fd

        def _release() -> None:
            global _LOCK_FD
            try:
                if _LOCK_FD is not None:
                    fcntl.flock(_LOCK_FD.fileno(), fcntl.LOCK_UN)
                    _LOCK_FD.close()
            except Exception:  # noqa: BLE001
                pass
            _LOCK_FD = None

        atexit.register(_release)
        _IS_BG = True
        logger.info("background role acquired pid=%s lock=%s", os.getpid(), path)
        return True
    except BlockingIOError:
        _IS_BG = False
        logger.info("background role skipped pid=%s (another worker holds lock)", os.getpid())
        return False
    except Exception:  # noqa: BLE001
        # If lock fails (e.g. read-only /tmp), run bg anyway so loops still exist.
        logger.warning("bg lock failed — running background loops here", exc_info=True)
        _IS_BG = True
        return True


def is_background_process() -> bool:
    if _IS_BG is None:
        return acquire_background_role()
    return bool(_IS_BG)
