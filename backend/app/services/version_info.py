"""Local panel version from VERSION file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CANDIDATES = (
    Path("/opt/astra/VERSION"),
    _REPO_ROOT / "VERSION",
    Path(__file__).resolve().parents[2] / "VERSION",
)


@lru_cache(maxsize=1)
def get_version() -> str:
    for p in _CANDIDATES:
        try:
            if p.is_file():
                v = p.read_text(encoding="utf-8").strip().splitlines()[0].strip()
                if v:
                    return v
        except OSError:
            continue
    return "1.0.0"


def clear_version_cache() -> None:
    get_version.cache_clear()
