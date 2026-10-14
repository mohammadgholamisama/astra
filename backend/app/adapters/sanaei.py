"""
3x-ui / Sanaei adapter — per official docs.sanaei.dev

Auth for programmatic API:
  Authorization: Bearer <API Token>   (Settings → Security → API Token)

Username/password are also stored (panel login credentials) but Bearer token
is required for reliable /panel/api/* access (CSRF-safe).
"""

from __future__ import annotations

import json
import secrets
import uuid as uuid_lib
from typing import Any
from app.adapters.base import AdapterResult, BasePanelAdapter, UserPayload
from app.adapters.http_utils import err_msg, make_client, ok_json


class SanaeiAdapter(BasePanelAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = None
        self._cookies = None
        self._csrf: str | None = None
        # Optional path prefix already included in base_url by user

    def _require_token(self) -> str:
        token = (self.api_token or "").strip()
        if not token:
            raise RuntimeError(
                "برای سنایی (3x-ui) API Token الزامی است — از Settings → Security → API Token کپی کنید"
            )
        return token

    async def _get_client(self):
        if self._client is None:
            self._client = make_client(self.base_url, self.meta.get("verify_ssl", False))
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _bearer_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._require_token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _cookie_login(self) -> None:
        """Optional session login (username/password) for legacy panels without token routes."""
        if not self.username or not self.password:
            return
        client = await self._get_client()
        # 1) CSRF cookie (newer 3x-ui)
        try:
            csrf_resp = await client.get("/getCSRFToken")
            if csrf_resp.status_code == 200:
                data = csrf_resp.json()
                self._csrf = (data.get("obj") or data.get("csrf") or data.get("token") or "")
                if isinstance(self._csrf, dict):
                    self._csrf = self._csrf.get("token")
        except Exception:
            self._csrf = None

        headers: dict[str, str] = {"Accept": "application/json"}
        if self._csrf:
            headers["X-CSRF-Token"] = str(self._csrf)

        resp = await client.post(
            "/login",
            json={"username": self.username, "password": self.password},
            headers=headers,
        )
        # Some versions expect form body
        if resp.status_code >= 400:
            resp = await client.post(
                "/login",
                data={"username": self.username, "password": self.password},
                headers=headers,
            )
        self._cookies = resp.cookies
        ok, data = ok_json(resp)
        if not ok:
            # Cookie login is best-effort when Bearer exists
            if self.api_token:
                return
            raise RuntimeError(err_msg(data, "Sanaei login failed"))

    async def _request(self, method: str, path: str, **kwargs: Any):
        client = await self._get_client()
        headers = {**self._bearer_headers(), **kwargs.pop("headers", {})}
        # Prefer Bearer-only (docs: CSRF skipped for Bearer on /panel/api/*)
        resp = await client.request(method, path, headers=headers, **kwargs)
        if resp.status_code in (401, 403) and self.username and self.password:
            await self._cookie_login()
            if self._csrf:
                headers["X-CSRF-Token"] = str(self._csrf)
            resp = await client.request(method, path, headers=headers, cookies=self._cookies, **kwargs)
        return resp

    def _client_payload(self, payload: UserPayload) -> dict[str, Any]:
        email = payload.username
        sub_id = payload.config.get("sub_id") or secrets.token_hex(8)
        client_uuid = payload.uuid or str(uuid_lib.uuid4())
        expiry_ms = int(payload.expire * 1000) if payload.expire else 0
        tg = payload.config.get("tg_id", 0)
        try:
            tg_id = int(tg) if tg not in (None, "") else 0
        except (TypeError, ValueError):
            tg_id = 0
        return {
            "email": email,
            "id": client_uuid,
            "subId": sub_id,
            "totalGB": int(payload.data_limit or 0),  # bytes per 3x-ui docs/examples
            "expiryTime": expiry_ms,
            "enable": payload.status in ("active", "on_hold"),
            "tgId": tg_id,
            "limitIp": int(payload.config.get("limit_ip", 0) or 0),
            "flow": payload.config.get("flow", "") or "",
            "comment": payload.note or "",
        }

    async def list_inbounds(self) -> list[dict[str, Any]]:
        """Fetch all inbounds from remote panel (ignores local meta filter)."""
        self._require_token()
        resp = await self._request("GET", "/panel/api/inbounds/list")
        ok, data = ok_json(resp)
        if not ok:
            resp = await self._request("GET", "/panel/api/inbounds/list/slim")
            ok, data = ok_json(resp)
        if not ok:
            raise RuntimeError(err_msg(data, "Cannot list inbounds"))
        obj = data.get("obj") if isinstance(data, dict) else data
        if not isinstance(obj, list):
            return []
        out: list[dict[str, Any]] = []
        for item in obj:
            if not isinstance(item, dict) or "id" not in item:
                continue
            out.append(
                {
                    "id": int(item["id"]),
                    "remark": (item.get("remark") or item.get("tag") or "") or "",
                    "protocol": (item.get("protocol") or "") or "",
                    "port": item.get("port"),
                    "enable": bool(item.get("enable", True)),
                }
            )
        return out

    async def _list_inbound_ids(self) -> list[int]:
        configured = self.meta.get("inbound_ids") or []
        if configured:
            return [int(x) for x in configured]
        return [int(i["id"]) for i in await self.list_inbounds()]

    async def test_connection(self) -> AdapterResult:
        try:
            self._require_token()
            resp = await self._request("GET", "/panel/api/inbounds/list")
            ok, data = ok_json(resp)
            if ok:
                return AdapterResult(success=True, raw=data)
            resp2 = await self._request("GET", "/panel/api/clients/list")
            ok2, data2 = ok_json(resp2)
            if ok2:
                return AdapterResult(success=True, raw=data2)
            return AdapterResult(success=False, error=err_msg(data, f"HTTP {resp.status_code}"))
        except Exception as e:
            return AdapterResult(success=False, error=str(e))

    async def create_user(self, payload: UserPayload) -> AdapterResult:
        try:
            client_obj = self._client_payload(payload)
            inbound_ids = payload.config.get("inbound_ids") or await self._list_inbound_ids()
            if not inbound_ids:
                return AdapterResult(success=False, error="هیچ inbound فعالی در سنایی پیدا نشد")

            # Modern API (3x-ui v3+)
            resp = await self._request(
                "POST",
                "/panel/api/clients/add",
                json={"client": client_obj, "inboundIds": inbound_ids},
            )
            ok, data = ok_json(resp)
            if ok:
                links = await self._fetch_links(client_obj["email"], client_obj["subId"])
                return AdapterResult(
                    success=True,
                    remote_username=client_obj["email"],
                    remote_id=client_obj["email"],
                    uuid=client_obj["id"],
                    links=links,
                    raw=data,
                )

            # Legacy fallback: /panel/api/inbounds/addClient
            settings_payload = json.dumps({"clients": [client_obj]})
            last_err = err_msg(data, "modern add failed")
            for iid in inbound_ids:
                legacy = await self._request(
                    "POST",
                    "/panel/api/inbounds/addClient",
                    json={"id": iid, "settings": settings_payload},
                )
                lok, ldata = ok_json(legacy)
                if lok:
                    links = await self._fetch_links(client_obj["email"], client_obj["subId"])
                    return AdapterResult(
                        success=True,
                        remote_username=client_obj["email"],
                        remote_id=client_obj["email"],
                        uuid=client_obj["id"],
                        links=links,
                        raw=ldata,
                    )
                last_err = err_msg(ldata, last_err)

            # Older multi-inbound endpoint
            multi = await self._request(
                "POST",
                "/panel/api/inbounds/addClientInbounds",
                json={"settings": settings_payload, "inboundIds": inbound_ids},
            )
            mok, mdata = ok_json(multi)
            if mok:
                links = await self._fetch_links(client_obj["email"], client_obj["subId"])
                return AdapterResult(
                    success=True,
                    remote_username=client_obj["email"],
                    remote_id=client_obj["email"],
                    uuid=client_obj["id"],
                    links=links,
                    raw=mdata,
                )
            return AdapterResult(success=False, error=err_msg(mdata, last_err))
        except Exception as e:
            return AdapterResult(success=False, error=str(e))

    async def modify_user(self, payload: UserPayload) -> AdapterResult:
        try:
            client_obj = self._client_payload(payload)
            # Full replace by email (modern)
            resp = await self._request(
                "POST",
                f"/panel/api/clients/update/{payload.username}",
                json=client_obj,
            )
            ok, data = ok_json(resp)
            if ok:
                links = await self._fetch_links(payload.username, client_obj["subId"])
                return AdapterResult(
                    success=True,
                    remote_username=payload.username,
                    remote_id=payload.username,
                    uuid=client_obj["id"],
                    links=links,
                    raw=data,
                )

            # Legacy updateClient by uuid
            uuid = payload.uuid or client_obj["id"]
            inbound_ids = payload.config.get("inbound_ids") or await self._list_inbound_ids()
            settings_payload = json.dumps({"clients": [client_obj]})
            for iid in inbound_ids:
                r = await self._request(
                    "POST",
                    f"/panel/api/inbounds/updateClient/{uuid}",
                    json={"id": iid, "settings": settings_payload},
                )
                rok, rdata = ok_json(r)
                if rok:
                    return AdapterResult(success=True, remote_username=payload.username, uuid=uuid, raw=rdata)
            return AdapterResult(success=False, error=err_msg(data, f"HTTP {resp.status_code}"))
        except Exception as e:
            return AdapterResult(success=False, error=str(e))

    async def delete_user(self, username: str, remote_id: str | None = None) -> AdapterResult:
        """
        Remove client from 3x-ui / Sanaei.
        Tries modern email delete, then legacy per-inbound UUID delete.
        """
        try:
            email = remote_id or username
            last_err = "delete failed"

            # Modern
            resp = await self._request("POST", f"/panel/api/clients/del/{email}")
            ok, data = ok_json(resp)
            if ok:
                return AdapterResult(success=True, raw=data)
            last_err = err_msg(data, f"HTTP {resp.status_code}")
            # already gone
            low = last_err.lower()
            if any(x in low for x in ("not found", "404", "does not exist")):
                return AdapterResult(success=True, raw=data)

            # Legacy: delete by UUID from each inbound
            inbound_ids = await self._list_inbound_ids()
            # Try to discover client UUID via traffic endpoint
            uuid = None
            try:
                t = await self._request("GET", f"/panel/api/clients/traffic/{email}")
                tok, tdata = ok_json(t)
                if tok and isinstance(tdata, dict):
                    obj = tdata.get("obj") or tdata
                    if isinstance(obj, dict):
                        uuid = obj.get("id") or obj.get("uuid")
            except Exception:  # noqa: BLE001
                pass

            if uuid and inbound_ids:
                any_ok = False
                for iid in inbound_ids:
                    r = await self._request(
                        "POST",
                        f"/panel/api/inbounds/{iid}/delClient/{uuid}",
                    )
                    rok, rdata = ok_json(r)
                    if rok:
                        any_ok = True
                    else:
                        last_err = err_msg(rdata, last_err)
                if any_ok:
                    return AdapterResult(success=True)

            return AdapterResult(success=False, error=last_err)
        except Exception as e:
            return AdapterResult(success=False, error=str(e))

    async def get_usage(self, username: str, remote_id: str | None = None) -> AdapterResult:
        try:
            email = remote_id or username
            # Modern
            resp = await self._request("GET", f"/panel/api/clients/traffic/{email}")
            ok, data = ok_json(resp)
            if ok and isinstance(data, dict):
                obj = data.get("obj") or data
                if isinstance(obj, dict):
                    up = int(obj.get("up") or 0)
                    down = int(obj.get("down") or 0)
                    return AdapterResult(success=True, remote_username=email, used_traffic=up + down, raw=data)
            # Legacy
            resp2 = await self._request("GET", f"/panel/api/inbounds/getClientTraffics/{email}")
            ok2, data2 = ok_json(resp2)
            if ok2 and isinstance(data2, dict):
                obj = data2.get("obj") or {}
                up = int(obj.get("up") or 0)
                down = int(obj.get("down") or 0)
                return AdapterResult(success=True, remote_username=email, used_traffic=up + down, raw=data2)
            return AdapterResult(success=False, error=err_msg(data, f"HTTP {resp.status_code}"))
        except Exception as e:
            return AdapterResult(success=False, error=str(e))

    async def _fetch_links(self, email: str, sub_id: str | None = None) -> list[str]:
        links: list[str] = []
        try:
            resp = await self._request("GET", f"/panel/api/clients/links/{email}")
            ok, data = ok_json(resp)
            if ok and isinstance(data, dict):
                obj = data.get("obj") or data
                links.extend(self._extract_links(obj))
        except Exception:
            pass
        if sub_id:
            try:
                resp = await self._request("GET", f"/panel/api/clients/subLinks/{sub_id}")
                ok, data = ok_json(resp)
                if ok and isinstance(data, dict):
                    links.extend(self._extract_links(data.get("obj") or data))
            except Exception:
                pass
        # unique preserve order
        seen: set[str] = set()
        out: list[str] = []
        for L in links:
            if L and L not in seen:
                seen.add(L)
                out.append(L)
        return out

    def _extract_links(self, obj: Any) -> list[str]:
        found: list[str] = []
        if isinstance(obj, str) and "://" in obj:
            found.append(obj)
        elif isinstance(obj, list):
            for item in obj:
                found.extend(self._extract_links(item))
        elif isinstance(obj, dict):
            for v in obj.values():
                found.extend(self._extract_links(v))
        return found

    async def get_subscription_links(self, username: str, remote_id: str | None = None) -> AdapterResult:
        try:
            email = remote_id or username
            links = await self._fetch_links(email)
            return AdapterResult(success=True, remote_username=email, links=links)
        except Exception as e:
            return AdapterResult(success=False, error=str(e))
