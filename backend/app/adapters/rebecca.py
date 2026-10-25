"""Rebecca panel adapter — official Go API (current Rebecca).

Manual inbound/protocol selection was removed. Users are assigned to a Service:
  GET  /api/v2/services?limit=none
  POST /api/v2/users  { username, service_id, data_limit, expire:null, status, ... }

Configs/links come from the service hosts — no proxies/inbounds in the create body.

Important: DELETE is soft-delete. Recreating the same username returns 409 and
modify returns "User not found". Sample refresh must upsert (modify) instead of
delete+create.
"""

from __future__ import annotations

from typing import Any

from app.adapters.base import AdapterResult, UserPayload
from app.adapters.http_utils import err_msg, expand_subscription_links, ok_json
from app.adapters.marzban import MarzbanAdapter


class RebeccaAdapter(MarzbanAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._services_cache: list[dict[str, Any]] | None = None

    async def list_services(self) -> list[dict[str, Any]]:
        if self._services_cache is not None:
            return self._services_cache
        client = await self._get_client()
        headers = await self._headers()
        last_err = "services unavailable"
        attempts = (
            ("/api/v2/services", {"limit": "none", "offset": "0"}),
            ("/api/v2/services", {"limit": "1000", "offset": "0"}),
            ("/api/v2/services", None),
        )
        for path, params in attempts:
            resp = await client.get(path, headers=headers, params=params)
            ok, data = ok_json(resp)
            if not ok:
                last_err = err_msg(data, f"HTTP {resp.status_code} {path}")
                continue
            rows = self._normalize_services_payload(data)
            if rows is not None:
                self._services_cache = rows
                return rows
        raise RuntimeError(last_err)

    def _normalize_services_payload(self, data: Any) -> list[dict[str, Any]] | None:
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            if isinstance(data.get("services"), list):
                items = data["services"]
            elif isinstance(data.get("items"), list):
                items = data["items"]
            else:
                return None
        else:
            return None
        out: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            try:
                sid = int(item["id"])
            except (TypeError, ValueError):
                continue
            if sid <= 0:
                continue
            broken = bool(item.get("broken") or False)
            host_count = int(item.get("host_count") or 0)
            out.append(
                {
                    "id": sid,
                    "name": str(item.get("name") or item.get("title") or f"service-{sid}"),
                    "description": item.get("description"),
                    "host_count": host_count,
                    "user_count": int(item.get("user_count") or 0),
                    "has_hosts": bool(item.get("has_hosts", host_count > 0)),
                    "broken": broken,
                    "is_disabled": broken,
                }
            )
        return out

    async def list_protocols(self) -> list[dict[str, Any]]:
        services = await self.list_services()
        out: list[dict[str, Any]] = []
        for svc in services:
            tag = f"service-{svc['id']}"
            out.append(
                {
                    "protocol": tag,
                    "tags": [tag],
                    "inbound_count": max(int(svc.get("host_count") or 0), 1),
                    "inbounds": [
                        {
                            "tag": tag,
                            "protocol": "service",
                            "network": "",
                            "tls": "",
                            "port": None,
                        }
                    ],
                    "service_id": svc["id"],
                    "service_name": svc["name"],
                    "is_disabled": bool(svc.get("is_disabled") or svc.get("broken")),
                }
            )
        return out

    def _selected_service_id(self, payload: UserPayload) -> int | None:
        raw = payload.config.get("service_id")
        if raw is None:
            raw = self.meta.get("service_id")
        if raw is not None and raw != "":
            try:
                sid = int(raw)
                if sid > 0:
                    return sid
            except (TypeError, ValueError):
                pass

        for key in ("service_ids",):
            arr = payload.config.get(key) or self.meta.get(key)
            if isinstance(arr, list):
                for x in arr:
                    try:
                        sid = int(x)
                        if sid > 0:
                            return sid
                    except (TypeError, ValueError):
                        continue

        protocols = payload.config.get("protocols")
        if isinstance(protocols, list):
            for p in protocols:
                s = str(p).strip().lower()
                if s.startswith("service-"):
                    try:
                        sid = int(s.split("-", 1)[1])
                        if sid > 0:
                            return sid
                    except (TypeError, ValueError, IndexError):
                        pass

        inbounds = payload.config.get("inbounds")
        if isinstance(inbounds, dict):
            for key, tags in inbounds.items():
                cands = [key] + (list(tags) if isinstance(tags, list) else [])
                for c in cands:
                    s = str(c).strip().lower()
                    if s.startswith("service-"):
                        try:
                            sid = int(s.split("-", 1)[1])
                            if sid > 0:
                                return sid
                        except (TypeError, ValueError, IndexError):
                            pass
        return None

    async def _resolve_service_id(self, payload: UserPayload) -> int:
        selected = self._selected_service_id(payload)
        try:
            catalog = await self.list_services()
        except Exception as exc:
            if selected:
                return selected
            raise RuntimeError(f"لیست سرویس‌های ربکا دریافت نشد: {exc}") from exc

        available = [int(s["id"]) for s in catalog if not s.get("is_disabled") and not s.get("broken")]
        if not available:
            available = [int(s["id"]) for s in catalog]

        if selected:
            if selected in available or selected in {int(s["id"]) for s in catalog}:
                return selected
            raise RuntimeError(f"سرویس انتخاب‌شده ({selected}) در پنل ربکا پیدا نشد")

        if available:
            return available[0]
        raise RuntimeError(
            "در پنل ربکا هیچ سرویسی تعریف نشده — اول در داشبورد ربکا یک Service بسازید"
        )

    def _map_rebecca_status(self, payload: UserPayload, *, for_create: bool) -> tuple[str, int | None]:
        """Rebecca status rules (probed on live Hajmi9_14):

        - CREATE: only ``active`` | ``on_hold`` (on_hold needs on_hold_expire_duration > 0).
          Sending ``disabled`` / ``limited`` / ``expired`` → ``invalid user status``.
        - MODIFY: ``active`` | ``disabled`` | ``on_hold`` (with duration).
          ``limited`` / ``expired`` are not writable — map to ``disabled``.
        """
        st = (payload.status or "active").lower().strip()
        if st in ("limited", "expired"):
            st = "disabled"
        if st not in ("active", "on_hold", "disabled"):
            st = "disabled"

        hold_dur: int | None = None
        raw_dur = (payload.config or {}).get("on_hold_expire_duration")
        try:
            hold_dur = int(raw_dur) if raw_dur is not None else None
        except (TypeError, ValueError):
            hold_dur = None
        if hold_dur is not None and hold_dur <= 0:
            hold_dur = None

        if for_create:
            # Create enum rejects disabled — create active then freeze via modify
            if st == "on_hold" and hold_dur:
                return "on_hold", hold_dur
            return "active", None

        if st == "on_hold":
            if not hold_dur:
                # Without duration Rebecca rejects on_hold; keep traffic cut via disabled
                return "disabled", None
            return "on_hold", hold_dur
        return st, None

    async def _build_user_body(self, payload: UserPayload, *, include_username: bool) -> dict[str, Any]:
        """Match Rebecca dashboard POST /api/v2/users body."""
        service_id = await self._resolve_service_id(payload)
        status, hold_dur = self._map_rebecca_status(payload, for_create=include_username)
        data_limit = int(payload.data_limit or 0)
        expire = int(payload.expire or 0) if payload.expire else 0
        # on_hold cannot combine with expire on Rebecca/Marzban
        if status == "on_hold":
            expire = 0
        body: dict[str, Any] = {
            "service_id": service_id,
            "status": status,
            "note": payload.note or "",
            "telegram_id": "",
            "contact_number": "",
            # Dashboard sends null for unlimited — not 0
            "data_limit": data_limit if data_limit > 0 else None,
            "data_limit_reset_strategy": payload.config.get("data_limit_reset_strategy", "no_reset"),
            "expire": expire if expire > 0 else None,
            "flow": None,
            "on_hold_expire_duration": hold_dur,
            "ip_limit": 0,
        }
        limit_ip = payload.config.get("limit_ip") or payload.config.get("ip_limit")
        if limit_ip is not None:
            try:
                n = int(limit_ip)
                if n >= 0:
                    body["ip_limit"] = n
            except (TypeError, ValueError):
                pass
        if include_username:
            body["username"] = payload.username
        return body

    def _from_user_response(self, data: dict, fallback_username: str) -> AdapterResult:
        result = super()._from_user_response(data, fallback_username)
        if not result.uuid:
            ck = data.get("credential_key")
            if ck:
                result.uuid = str(ck)
            else:
                link_data = data.get("link_data")
                if isinstance(link_data, list):
                    for item in link_data:
                        if isinstance(item, dict) and item.get("uuid"):
                            result.uuid = str(item["uuid"])
                            break
        # Also collect subscription_urls map values
        links = list(result.links or [])
        subs = data.get("subscription_urls")
        if isinstance(subs, dict):
            for v in subs.values():
                if v and str(v) not in links:
                    links.append(str(v))
        result.links = links
        return result

    async def get_usage(
        self,
        username: str,
        remote_id: str | None = None,
        *,
        expand_links: bool = True,
    ) -> AdapterResult:
        """Prefer Rebecca dashboard v2; fall back to legacy /api/user."""
        import asyncio

        import httpx

        uname = (username or remote_id or "").strip()
        if not uname:
            return AdapterResult(success=False, error="username خالی است")
        last_err = "get usage failed"
        try:
            client = await self._get_client()
            headers = await self._headers()
            t = httpx.Timeout(20.0, connect=10.0)
            for path in (f"/api/v2/users/{uname}", f"/api/user/{uname}"):
                try:
                    resp = await client.get(path, headers=headers, timeout=t)
                except Exception as exc:  # noqa: BLE001
                    last_err = str(exc).strip() or exc.__class__.__name__
                    continue
                ok, data = ok_json(resp)
                if ok and isinstance(data, dict):
                    result = self._from_user_response(data, uname)
                    if expand_links:
                        try:
                            result.links = await asyncio.wait_for(
                                self._expand_subscription_links(list(result.links or [])),
                                timeout=8,
                            )
                        except Exception:
                            pass
                    return result
                last_err = err_msg(data, f"HTTP {resp.status_code} {path}")
                if resp.status_code in (401, 403):
                    self._token = None
                    break
            return AdapterResult(success=False, error=last_err)
        except Exception as e:
            return AdapterResult(success=False, error=str(e).strip() or e.__class__.__name__)

    async def _finalize_user_result(self, result: AdapterResult, username: str) -> AdapterResult:
        links = list(result.links or [])
        client = await self._get_client()
        if not any(str(l).lower().startswith(("vless://", "vmess://", "trojan://", "ss://")) for l in links):
            refreshed = await self.get_usage(username)
            if refreshed.success and refreshed.links:
                links = list(refreshed.links)
                result.used_traffic = refreshed.used_traffic or result.used_traffic
                result.uuid = refreshed.uuid or result.uuid
                result.raw = refreshed.raw or result.raw
        result.links = await expand_subscription_links(client, links)
        return result

    def _is_conflict(self, err: str) -> bool:
        low = (err or "").lower()
        return any(x in low for x in ("already", "exist", "duplicate", "تکرار", "موجود"))

    def _soft_delete_trap_message(self, username: str) -> str:
        return (
            f"کاربر «{username}» در ربکا soft-delete شده و همان نام قابل ساخت/ویرایش نیست. "
            "در داشبورد ربکا آن کاربر را کامل پاک کنید، یا تازه‌سازی را دوباره بزنید تا نام نمونه عوض شود."
        )

    async def create_user(self, payload: UserPayload) -> AdapterResult:
        """Upsert — never rely on delete+create (Rebecca soft-delete traps the username)."""
        try:
            existing = await self.get_usage(payload.username)
            if existing.success:
                mod = await self.modify_user(payload)
                if mod.success:
                    return mod

            client = await self._get_client()
            headers = await self._headers()
            body = await self._build_user_body(payload, include_username=True)
            # Prefer v2 (dashboard); fall back to legacy /api/user
            last_err = "create failed"
            data: Any = None
            for path in ("/api/v2/users", "/api/user"):
                resp = await client.post(path, headers=headers, json=body)
                ok, data = ok_json(resp)
                if ok and isinstance(data, dict):
                    result = self._from_user_response(data, payload.username)
                    result = await self._finalize_user_result(result, payload.username)
                    # Created as active (create API rejects disabled). If Astra
                    # user is limited/expired/disabled, freeze via modify.
                    want, _ = self._map_rebecca_status(payload, for_create=False)
                    if want == "disabled" and result.success:
                        frozen = await self.modify_user(payload)
                        if frozen.success:
                            return frozen
                    return result
                last_err = err_msg(data, f"HTTP {resp.status_code} {path}")
                # Don't try legacy if v2 clearly rejected the payload/auth
                if resp.status_code in (401, 403, 422):
                    break

            err = last_err
            if self._is_conflict(err):
                mod = await self.modify_user(payload)
                if mod.success:
                    return mod
                # Soft-deleted username: stuck in DB, not visible to GET/PUT
                return AdapterResult(
                    success=False,
                    error=self._soft_delete_trap_message(payload.username),
                )
            return AdapterResult(success=False, error=err)
        except Exception as e:
            return AdapterResult(success=False, error=str(e))

    async def modify_user(self, payload: UserPayload) -> AdapterResult:
        try:
            client = await self._get_client()
            headers = await self._headers()
            body = await self._build_user_body(payload, include_username=False)
            last_err = "modify failed"
            for path in (
                f"/api/v2/users/{payload.username}",
                f"/api/user/{payload.username}",
            ):
                resp = await client.put(path, headers=headers, json=body)
                ok, data = ok_json(resp)
                if ok and isinstance(data, dict):
                    result = self._from_user_response(data, payload.username)
                    return await self._finalize_user_result(result, payload.username)
                last_err = err_msg(data, f"HTTP {resp.status_code} {path}")
                if resp.status_code in (401, 403, 404):
                    # 404 on v2 → try legacy; 404 on legacy → stop
                    continue
            return AdapterResult(success=False, error=last_err)
        except Exception as e:
            return AdapterResult(success=False, error=str(e))

    async def delete_user(self, username: str, remote_id: str | None = None) -> AdapterResult:
        # Soft-delete on Rebecca permanently burns the username for recreate.
        # Sample refresh skips this; real user-delete from Astra still calls it.
        return await super().delete_user(username, remote_id)
