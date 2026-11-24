"""Build a self-contained tarball for remote worker install."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path


def _astra_root() -> Path:
    # .../astra/backend/app/services/worker_bundle.py → astra/
    return Path(__file__).resolve().parents[3]


def build_worker_tarball() -> bytes:
    """Pack worker package + vendored ``app`` (adapters) for /opt/astra-worker."""
    root = _astra_root()
    worker_dir = root / "worker"
    app_dir = root / "backend" / "app"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if worker_dir.is_dir():
            for path in worker_dir.rglob("*"):
                if not path.is_file():
                    continue
                if "__pycache__" in path.parts or path.suffix == ".pyc":
                    continue
                arc = Path("worker") / path.relative_to(worker_dir)
                tar.add(path, arcname=str(arc))
        # Vendor backend app tree (adapters + deps). Worker sets PYTHONPATH=/opt/astra-worker
        if app_dir.is_dir():
            for path in app_dir.rglob("*"):
                if not path.is_file():
                    continue
                if "__pycache__" in path.parts or path.suffix == ".pyc":
                    continue
                # Skip heavy / irrelevant trees for agent
                rel = path.relative_to(app_dir)
                parts = rel.parts
                if parts and parts[0] in {"static", "templates"}:
                    continue
                arc = Path("app") / rel
                tar.add(path, arcname=str(arc))
        # Worker pip requirements (superset of agent needs)
        req = (
            "fastapi>=0.110\n"
            "uvicorn[standard]>=0.27\n"
            "httpx>=0.27\n"
            "pydantic>=2.5\n"
            "pydantic-settings>=2.0\n"
            "sqlalchemy[asyncio]>=2.0\n"
            "aiosqlite>=0.20\n"
            "orjson>=3.9\n"
            "python-multipart>=0.0.9\n"
            "email-validator>=2.0\n"
            "psutil>=5.9\n"
        )
        info = tarfile.TarInfo(name="requirements.txt")
        data = req.encode("utf-8")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()
