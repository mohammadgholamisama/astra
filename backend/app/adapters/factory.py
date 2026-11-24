from app.adapters.astra import AstraAdapter
from app.adapters.base import BasePanelAdapter
from app.adapters.guard import GuardAdapter
from app.adapters.marzban import MarzbanAdapter
from app.adapters.pasarguard import PasarGuardAdapter
from app.adapters.rebecca import RebeccaAdapter
from app.adapters.sanaei import SanaeiAdapter
from app.models.panel import Panel, PanelType

_ADAPTERS = {
    PanelType.marzban: MarzbanAdapter,
    PanelType.pasarguard: PasarGuardAdapter,
    PanelType.rebecca: RebeccaAdapter,
    PanelType.guard: GuardAdapter,
    PanelType.sanaei: SanaeiAdapter,
    PanelType.astra: AstraAdapter,
}


def _build_local(panel: Panel) -> BasePanelAdapter:
    cls = _ADAPTERS.get(panel.type)
    if not cls:
        raise ValueError(f"Unsupported panel type: {panel.type}")
    meta = dict(panel.meta or {})
    # Astra adapter resolves the panel row by id
    meta["panel_id"] = int(panel.id) if panel.id is not None else meta.get("panel_id")
    token = panel.api_token or str(meta.get("node_token") or "") or None
    return cls(
        base_url=panel.base_url,
        username=panel.username,
        password=panel.password,
        api_token=token,
        meta=meta,
    )


def get_adapter(panel: Panel) -> BasePanelAdapter:
    """Return local adapter, or worker-routed proxy with local failover.

    Astra node panels always run on master (need local DB / xray) — never Worker.
    """
    local = _build_local(panel)
    if panel.type == PanelType.astra:
        return local
    wid = getattr(panel, "worker_id", None)
    if not wid:
        return local
    try:
        from app.services.worker_client import (
            WorkerRoutedAdapter,
            lookup_worker_route,
            register_worker_route,
        )

        route = lookup_worker_route(int(wid))
        if route is None:
            # Relationship may be loaded — seed route from ORM worker row
            worker = getattr(panel, "__dict__", {}).get("worker")
            if worker is not None and getattr(worker, "is_active", False):
                tok = str((getattr(worker, "meta", None) or {}).get("agent_token") or "")
                if tok:
                    register_worker_route(
                        int(wid),
                        str(worker.base_url),
                        tok,
                        active=True,
                    )
                    route = lookup_worker_route(int(wid))
        if route is None:
            return local
        base_url, token = route
        return WorkerRoutedAdapter(  # type: ignore[return-value]
            panel,
            local,
            worker_id=int(wid),
            worker_base_url=base_url,
            worker_token=token,
        )
    except Exception:  # noqa: BLE001
        return local
