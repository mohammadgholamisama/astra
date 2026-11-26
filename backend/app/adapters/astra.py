"""Astra local node adapter — outbounds pool + metered VLESS entry."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.adapters.base import AdapterResult, BasePanelAdapter, UserPayload
from app.core.database import AsyncSessionLocal
from app.models.panel import Panel, PanelType
from app.services import astra_node as node

logger = logging.getLogger("astra.adapter.astra")


class AstraAdapter(BasePanelAdapter):
    """Talks to in-process Astra node (DB + Xray config), not a foreign panel API."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._panel_id: int | None = None
        # meta may carry panel_id when constructed from get_adapter
        try:
            self._panel_id = int((self.meta or {}).get("panel_id") or 0) or None
        except (TypeError, ValueError):
            self._panel_id = None

    async def _load_panel(self) -> Panel:
        def _detach(db, p: Panel) -> Panel:
            # Touch attrs then expunge so callers survive session close/rollback.
            _ = (p.id, p.type, p.base_url, p.meta, p.is_active, p.name)
            try:
                db.expunge(p)
            except Exception:  # noqa: BLE001
                pass
            return p

        if self._panel_id:
            async with AsyncSessionLocal() as db:
                p = await db.get(Panel, self._panel_id)
                if p and p.type == PanelType.astra:
                    return _detach(db, p)
        # Fallback: match by base_url + token
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Panel).where(Panel.type == PanelType.astra)
            )
            panels = list(result.scalars().all())
            for p in panels:
                if (p.base_url or "").rstrip("/") == (self.base_url or "").rstrip("/"):
                    tok = str((p.meta or {}).get("node_token") or "")
                    if not self.api_token or tok == (self.api_token or ""):
                        self._panel_id = p.id
                        return _detach(db, p)
            if len(panels) == 1:
                self._panel_id = panels[0].id
                return _detach(db, panels[0])
        raise RuntimeError("پنل Astra پیدا نشد — panel_id در meta لازم است")

    async def test_connection(self) -> AdapterResult:
        """Accurate node + outbound probe — never rebuild Xray (test-all safe).

        Local listen alone is not enough: if every enabled outbound is down,
        report قطع so operators don't see false «وصل».
        """
        try:
            panel = await self._load_panel()
            health = dict(node.node_health(panel) or {})
            if not health.get("listen_open"):
                if health.get("config_exists"):
                    return AdapterResult(
                        success=False,
                        error="نود Astra قطع است — config هست ولی xray گوش نمی‌دهد",
                        raw=health,
                    )
                return AdapterResult(
                    success=False,
                    error="نود Astra آماده نیست (config/xray)",
                    raw=health,
                )

            # test-all sets _probe_quick — prefer recent bg pings to avoid
            # stampede false «قطع» while staying accurate when cache is stale.
            quick = bool((self.meta or {}).get("_probe_quick"))
            ob = await node.probe_panel_outbounds_health(
                int(panel.id),
                concurrency=2,
                ping_timeout=5.0 if not quick else 4.0,
                prefer_cache_sec=180.0 if quick else 0.0,
            )
            health["outbounds"] = ob
            enabled = int(ob.get("enabled") or 0)
            ok_n = int(ob.get("ok") or 0)
            bad_n = int(ob.get("bad") or 0)
            disabled_n = int(ob.get("disabled") or 0)
            bad_names = list(ob.get("bad_names") or [])
            disabled_names = list(ob.get("disabled_names") or [])
            names_off = node._format_outbound_names(disabled_names)
            names_bad = node._format_outbound_names(bad_names)
            if enabled <= 0:
                if disabled_n > 0 and names_off:
                    return AdapterResult(
                        success=False,
                        error=f"اوتباند خاموش است: {names_off}",
                        raw=health,
                    )
                return AdapterResult(
                    success=False,
                    error="اوتباند فعالی روی این نود نیست",
                    raw=health,
                )
            if ok_n <= 0:
                if names_bad:
                    return AdapterResult(
                        success=False,
                        error=f"اوتباند قطع است (پینگ ناموفق): {names_bad}",
                        raw=health,
                    )
                return AdapterResult(
                    success=False,
                    error=f"اوتباند قطع است ({bad_n}/{enabled})",
                    raw=health,
                )
            # Partial: still «وصل» but warn which hops are down/off so the
            # panels toast/test-all list shows the live outbound names.
            warn_bits: list[str] = []
            if names_bad:
                warn_bits.append(f"قطع: {names_bad}")
            if names_off:
                warn_bits.append(f"خاموش: {names_off}")
            if warn_bits:
                health["outbound_warning"] = " · ".join(warn_bits)
            return AdapterResult(
                success=True,
                raw=health,
                error=None,
            )
        except Exception as e:  # noqa: BLE001
            return AdapterResult(
                success=False, error=str(e).strip() or e.__class__.__name__
            )

    async def create_user(self, payload: UserPayload) -> AdapterResult:
        return await self._upsert(payload)

    async def modify_user(self, payload: UserPayload) -> AdapterResult:
        return await self._upsert(payload)

    async def _upsert(self, payload: UserPayload) -> AdapterResult:
        try:
            panel = await self._load_panel()
            enable = (payload.status or "active").lower() in ("active", "on_hold")
            limit_ip = 0
            cfg = payload.config or {}
            raw_ip = cfg.get("limit_ip", cfg.get("ip_limit", 0))
            try:
                limit_ip = max(0, int(raw_ip or 0))
            except (TypeError, ValueError):
                limit_ip = 0
            # Batch flags from orchestrator (avoid N parallel nginx reloads)
            sync_nginx = not bool(cfg.get("_astra_skip_nginx"))
            do_rebuild = not bool(cfg.get("_astra_skip_rebuild"))
            async with AsyncSessionLocal() as db:
                p = await db.get(Panel, panel.id)
                if not p:
                    return AdapterResult(success=False, error="panel missing")
                client = await node.upsert_client(
                    db,
                    p,
                    email=payload.username,
                    uuid=payload.uuid,
                    data_limit=int(payload.data_limit or 0),
                    expire=int(payload.expire or 0),
                    enable=enable,
                    limit_ip=limit_ip,
                    rebuild=do_rebuild,
                    sync_nginx=sync_nginx,
                )
                _c, links, used = await node.client_usage_and_links(
                    db, p, payload.username
                )
                await db.commit()
            return AdapterResult(
                success=True,
                remote_username=payload.username,
                remote_id=str(client.id),
                uuid=client.uuid,
                links=links,
                used_traffic=used,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("astra upsert failed")
            return AdapterResult(success=False, error=str(e).strip() or e.__class__.__name__)

    async def delete_user(self, username: str, remote_id: str | None = None) -> AdapterResult:
        try:
            panel = await self._load_panel()
            async with AsyncSessionLocal() as db:
                p = await db.get(Panel, panel.id)
                if not p:
                    return AdapterResult(success=True)
                await node.delete_client(db, p, username)
                await db.commit()
            return AdapterResult(success=True, remote_username=username, remote_id=remote_id)
        except Exception as e:  # noqa: BLE001
            return AdapterResult(success=False, error=str(e).strip() or e.__class__.__name__)

    async def get_usage(self, username: str, remote_id: str | None = None) -> AdapterResult:
        try:
            panel = await self._load_panel()
            async with AsyncSessionLocal() as db:
                p = await db.get(Panel, panel.id)
                if not p:
                    return AdapterResult(success=False, error="panel missing")
                try:
                    # Coalesced — never rebuild xray on the get_usage hot path.
                    await node.sync_traffic_from_xray(
                        db, p, rebuild_on_limit=False
                    )
                except Exception:  # noqa: BLE001
                    pass
                client, links, used = await node.client_usage_and_links(
                    db, p, username
                )
                await db.commit()
            if not client:
                return AdapterResult(success=False, error="user not found")
            return AdapterResult(
                success=True,
                remote_username=username,
                remote_id=str(client.id),
                uuid=client.uuid,
                links=links,
                used_traffic=used,
                remote_expire=int(client.expire or 0) or None,
                remote_status="active" if client.enable else "disabled",
            )
        except Exception as e:  # noqa: BLE001
            return AdapterResult(success=False, error=str(e).strip() or e.__class__.__name__)

    async def get_subscription_links(
        self, username: str, remote_id: str | None = None
    ) -> AdapterResult:
        return await self.get_usage(username, remote_id)

    async def reset_usage(self, username: str, remote_id: str | None = None) -> AdapterResult:
        try:
            panel = await self._load_panel()
            async with AsyncSessionLocal() as db:
                p = await db.get(Panel, panel.id)
                if not p:
                    return AdapterResult(success=False, error="panel missing")
                # Prefer remote username; also try bare name if alias mismatch.
                # Skip nginx here — orchestrator / next modify refreshes once.
                client = await node.reset_client_usage(
                    db, p, username, re_enable=True, sync_nginx=False
                )
                if not client and remote_id:
                    client = await node.reset_client_usage(
                        db, p, str(remote_id), re_enable=True, sync_nginx=False
                    )
                await db.commit()
            if not client:
                return AdapterResult(success=False, error="user not found")
            return AdapterResult(
                success=True,
                remote_username=client.email or username,
                remote_id=str(client.id),
                used_traffic=0,
            )
        except Exception as e:  # noqa: BLE001
            return AdapterResult(success=False, error=str(e).strip() or e.__class__.__name__)
