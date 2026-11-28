# -*- coding: utf-8 -*-
"""Inspect + repair users stuck at partial «N/M پنل» (pending/empty-share).

Run on the live master (or via ship helper):

  cd /opt/astra/backend && .venv/bin/python - <<'PY'
  # or copy this file and:
  python /opt/astra/scripts/heal_incomplete_panels.py 320644576 8290367300d ...
  PY

Without args: heals the top partial users from the fleet.
With usernames: force-repairs those chips first (bypasses the 50k drain queue).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Allow: `.venv/bin/python /opt/astra/scripts/heal_incomplete_panels.py …`
_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if _BACKEND.is_dir() and str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


async def _amain(names: list[str], limit: int) -> int:
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.user import User
    from app.services.orchestrator import (
        complete_user_panel_coverage,
        repair_incomplete_panel_coverage,
    )

    if names:
        report = {"users": [], "repaired": 0, "still_partial": 0}
        async with AsyncSessionLocal() as db:
            rows = await db.execute(
                select(User.id, User.username).where(User.username.in_(names))
            )
            found = {str(r[1]): int(r[0]) for r in rows.all() if r[0]}
            await db.rollback()
        for n in names:
            uid = found.get(n)
            if not uid:
                report["users"].append(
                    {"username": n, "ok": False, "detail": "not_found"}
                )
                continue
            stats = await complete_user_panel_coverage(
                uid, max_rounds=5, round_budget=70.0
            )
            report["users"].append({"username": n, **stats})
            if stats.get("done"):
                report["repaired"] += 1
            else:
                report["still_partial"] += 1
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["still_partial"] else 0

    report = await repair_incomplete_panel_coverage(
        usernames=None,
        limit_users=limit,
        wait_per_user=90.0,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Repair incomplete panel coverage")
    ap.add_argument(
        "usernames",
        nargs="*",
        help="Optional usernames to repair (default: top fleet partials)",
    )
    ap.add_argument("--limit", type=int, default=40, help="Max users when no names")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(_amain(list(args.usernames), int(args.limit))))


if __name__ == "__main__":
    main()
