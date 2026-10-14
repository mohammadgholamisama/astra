from app.adapters.base import BasePanelAdapter
from app.adapters.marzban import MarzbanAdapter
from app.adapters.pasarguard import PasarGuardAdapter
from app.adapters.rebecca import RebeccaAdapter
from app.adapters.sanaei import SanaeiAdapter
from app.models.panel import Panel, PanelType

_ADAPTERS = {
    PanelType.marzban: MarzbanAdapter,
    PanelType.pasarguard: PasarGuardAdapter,
    PanelType.rebecca: RebeccaAdapter,
    PanelType.sanaei: SanaeiAdapter,
}


def get_adapter(panel: Panel) -> BasePanelAdapter:
    cls = _ADAPTERS.get(panel.type)
    if not cls:
        raise ValueError(f"Unsupported panel type: {panel.type}")
    return cls(
        base_url=panel.base_url,
        username=panel.username,
        password=panel.password,
        api_token=panel.api_token,
        meta=panel.meta or {},
    )
