"""Dedicated background process — no HTTP, only fleet/hot/usage loops.

Run via systemd ``astra-bg.service`` with ``ASTRA_ROLE=bg`` so API workers
(``ASTRA_ROLE=api``) stay light.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

# Force bg role before importing app.main helpers.
os.environ.setdefault("ASTRA_ROLE", "bg")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("astra.bg")


async def _amain() -> None:
    from app.core.database import AsyncSessionLocal, dispose_engine, pool_guardian_loop
    from app.core.process_role import acquire_background_role
    from app.core.schema_sync import ensure_schema
    from app.seed import seed_admin
    from app.services.config_hides import refresh_cache as refresh_config_hides
    from app.services.config_locations import refresh_cache as refresh_config_locations
    from app.services.config_orders import refresh_cache as refresh_config_orders
    from app.services.site_settings import refresh_cache as refresh_site_settings
    from app.services.usage_tiers import ensure_tier_loop
    from app.services.worker_lease import ensure_lease_loop
    from app.services.worker_ops import bootstrap_worker_routes

    # Import loops from main (same implementations).
    from app.main import (
        _analytics_sample_loop,
        _astra_xray_heal_loop,
        _auto_retry_errors_loop,
        _drain_recent_pending_provisions,
        _empty_user_coverage_heal_loop,
        _fleet_usage_loop,
        _hot_usage_loop,
        _outbound_health_loop,
        _priority_usage_loop,
        _usage_sync_loop,
        _worker_probe_loop,
    )

    if not acquire_background_role():
        logger.error("failed to acquire background role — exiting")
        return

    await ensure_schema()
    await seed_admin()
    async with AsyncSessionLocal() as db:
        await refresh_site_settings(db)
        await refresh_config_locations(db)
        await refresh_config_hides(db)
        await refresh_config_orders(db)
        try:
            n = await bootstrap_worker_routes(db)
            logger.info("bg worker routes n=%s", n)
        except Exception:  # noqa: BLE001
            logger.warning("worker route bootstrap failed", exc_info=True)
        await db.rollback()

    # Affinity lease rebalance + Hot/Warm tier scanner (Master CPU stays light).
    try:
        await ensure_lease_loop()
        await ensure_tier_loop()
        logger.info("bg lease+tier loops started")
    except Exception:  # noqa: BLE001
        logger.warning("lease/tier loop start failed", exc_info=True)

    # usage-report queue stays on API processes (in-memory coalesce + dirty set).

    async def _delayed(coro, delay: float):
        await asyncio.sleep(delay)
        await coro()

    tasks = [
        asyncio.create_task(_delayed(_usage_sync_loop, 90), name="astra-usage-sync"),
        asyncio.create_task(_delayed(_fleet_usage_loop, 75), name="astra-fleet-usage"),
        asyncio.create_task(_delayed(_hot_usage_loop, 40), name="astra-hot-usage"),
        asyncio.create_task(
            _delayed(_priority_usage_loop, 35), name="astra-priority-usage"
        ),
        asyncio.create_task(
            _delayed(_worker_probe_loop, 20), name="astra-worker-probe"
        ),
        asyncio.create_task(
            _delayed(_analytics_sample_loop, 25), name="astra-analytics-sample"
        ),
        asyncio.create_task(_auto_retry_errors_loop(), name="astra-auto-retry-errors"),
        asyncio.create_task(
            _delayed(_outbound_health_loop, 120), name="astra-outbound-health"
        ),
        asyncio.create_task(
            _delayed(_astra_xray_heal_loop, 240), name="astra-xray-heal"
        ),
        # Durable create/bulk safety nets — survive API worker restarts.
        asyncio.create_task(
            _drain_recent_pending_provisions(), name="astra-pending-drain"
        ),
        asyncio.create_task(
            _empty_user_coverage_heal_loop(), name="astra-empty-coverage-heal"
        ),
        asyncio.create_task(pool_guardian_loop(), name="astra-db-pool-guard"),
    ]
    logger.info("astra-bg running tasks=%s", len(tasks))

    stop = asyncio.Event()

    def _stop(*_args) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    await stop.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await dispose_engine()
    logger.info("astra-bg stopped")


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
