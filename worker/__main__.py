"""python -m worker --host 0.0.0.0 --port 2090"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> None:
    # Allow /opt/astra-worker/app (vendored adapters) on sys.path
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    parser = argparse.ArgumentParser(description="Astra Worker agent")
    parser.add_argument("--host", default=os.environ.get("ASTRA_WORKER_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ASTRA_WORKER_PORT", "2090")),
    )
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        "worker.app:app",
        host=args.host,
        port=args.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
