"""Per-panel subscription rank + ping-test status for integration API."""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.panel import Panel
from app.models.worker import WorkerNode
from app.schemas.integration import (
    PanelConfigTestOut,
    PanelSubStatusOut,
    PanelsSubStatusResponse,
)
from app.services import config_orders as order_svc


async def panels_sub_status(
    db: AsyncSession, *, panel_id: int | None = None
) -> PanelsSubStatusResponse:
    """Build per-panel view of config-order ranks and ping test results."""
    workspace = await order_svc.list_workspace(db)
    items = list(workspace.items or [])

    q = select(Panel).order_by(Panel.id.asc())
    if panel_id is not None:
        q = q.where(Panel.id == int(panel_id))
    panels = list((await db.execute(q)).scalars().all())

    worker_ids = {
        int(p.worker_id)
        for p in panels
        if getattr(p, "worker_id", None) is not None
    }
    workers: dict[int, str] = {}
    if worker_ids:
        rows = (
            await db.execute(
                select(WorkerNode.id, WorkerNode.name).where(
                    WorkerNode.id.in_(list(worker_ids))
                )
            )
        ).all()
        workers = {int(i): str(n) for i, n in rows}

    by_name: dict[str, Panel] = {}
    for p in panels:
        by_name[str(p.name or "").strip()] = p

    # Map panel_id → config rows (match by panel_names on order items).
    bucket: dict[int, list[PanelConfigTestOut]] = defaultdict(list)
    for it in items:
        rank = int(getattr(it, "sort_order", 0) or 0) + 1
        avg = getattr(it, "avg_ping", None)
        try:
            avg_f = float(avg) if avg is not None else None
        except (TypeError, ValueError):
            avg_f = None
        if avg_f is not None and avg_f < 0:
            avg_f = None
        tested = avg_f is not None
        conf = PanelConfigTestOut(
            host=str(it.host),
            port=int(it.port),
            match_host=str(getattr(it, "match_host", "") or ""),
            label=getattr(it, "label", None),
            protocol=getattr(it, "protocol", None),
            sub_rank=rank,
            avg_ping_ms=avg_f,
            device_count=getattr(it, "device_count", None),
            tested=tested,
            hidden=bool(getattr(it, "hidden", False)),
            group_names=list(getattr(it, "group_names", None) or []),
            category_name=getattr(it, "category_name", None),
            flag=getattr(it, "flag", None),
        )
        names = list(getattr(it, "panel_names", None) or [])
        matched = False
        for pname in names:
            p = by_name.get(str(pname or "").strip())
            if p is None:
                continue
            bucket[int(p.id)].append(conf)
            matched = True
        if not matched and panel_id is None:
            # Configs without panel_names stay global-only; skip orphan attach.
            pass

    out_panels: list[PanelSubStatusOut] = []
    for p in panels:
        configs = list(bucket.get(int(p.id), []))
        configs.sort(key=lambda c: (c.sub_rank, c.host, c.port))
        pings = [c.avg_ping_ms for c in configs if c.avg_ping_ms is not None]
        ranks = [c.sub_rank for c in configs]
        wid = int(p.worker_id) if getattr(p, "worker_id", None) else None
        out_panels.append(
            PanelSubStatusOut(
                panel_id=int(p.id),
                name=str(p.name),
                type=str(getattr(p.type, "value", p.type)),
                is_active=bool(p.is_active),
                worker_id=wid,
                worker_name=workers.get(wid) if wid else None,
                configs_count=len(configs),
                tested_count=len(pings),
                best_ping_ms=min(pings) if pings else None,
                worst_ping_ms=max(pings) if pings else None,
                best_sub_rank=min(ranks) if ranks else None,
                configs=configs,
            )
        )

    return PanelsSubStatusResponse(
        total_panels=len(out_panels),
        total_ordered_configs=len(items),
        panels=out_panels,
    )
