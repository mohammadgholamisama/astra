"""Guard panel adapter (erfjab / core.erfjab.com Management API).

Auth: JSON login POST /api/admins/token → Bearer JWT, or X-API-Key from api_token.
Users = subscriptions. Services act like Rebecca service_id catalog.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.adapters.base import AdapterResult, BasePanelAdapter, UserPayload, coerce_unix_ts
from app.adapters.http_utils import err_msg, expand_subscription_links, make_client, ok_json

_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TOKEN_TTL = 240.0


class GuardAdapter(BasePanelAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._token: str | None = None
        self._auth_mode: str = "bearer"  # bearer | api_key
        self._client: httpx.AsyncClient | None = None
        self._services_cache: list[dict[str, Any]] | None = None

    def _cache_key(self) -> str:
        return f"guard|{self.base_url}|{(self.username or '').strip()}|{(self.api_token or '')[:12]}"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = make_client(
                self.base_url,
                self.meta.get("verify_ssl", False),
                xhr=False,
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _ensure_token(self) -> str:
        if self._token:
            return self._token

        api_key = (self.api_token or "").strip()
        if api_key:
            self._token = api_key
            self._auth_mode = "api_key"
            return self._token

        user = (self.username or "").strip()
        password = (self.password or "").strip()
        if not user or not password:
            raise RuntimeError("گارد نیاز به یوزر/پسورد ادمین یا API Key دارد")

        cache_key = self._cache_key()
        cached = _TOKEN_CACHE.get(cache_key)
        if cached and time.monotonic() < cached[1]:
            self._token = cached[0]
            self._auth_mode = "bearer"
            return self._token

        client = await self._get_client()
        last_err = "Login failed"
        # Official docs: JSON body. Older OpexCore used form — try both.
        attempts: list[tuple[str, dict[str, Any] | None, dict[str, str] | None]] = [
            ("json", {"username": user, "password": password}, None),
            (
                "form",
                None,
                {
                    "grant_type": "password",
                    "username": user,
                    "password": password,
                    "scope": "",
                },
            ),
        ]
        for kind, json_body, form in attempts:
            try:
                if kind == "json":
                    resp = await client.post("/api/admins/token", json=json_body)
                else:
                    resp = await client.post(
                        "/api/admins/token",
                        data=form,
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc).strip() or exc.__class__.__name__
                continue
            ok, data = ok_json(resp)
            if ok and isinstance(data, dict):
                tok = data.get("token") or data.get("access_token")
                if tok:
                    self._token = str(tok)
                    self._auth_mode = "bearer"
                    _TOKEN_CACHE[cache_key] = (self._token, time.monotonic() + _TOKEN_TTL)
                    return self._token
            last_err = err_msg(data, f"Login HTTP {resp.status_code}")
            if resp.status_code in (401, 403):
                break
        raise RuntimeError(last_err)

    async def _headers(self) -> dict[str, str]:
        tok = await self._ensure_token()
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._auth_mode == "api_key":
            h["X-API-Key"] = tok
        else:
            h["Authorization"] = f"Bearer {tok}"
            # Some Guard builds also accept JWT as X-API-Key
            h["X-API-Key"] = tok
        return h

    def _invalidate_token(self) -> None:
        self._token = None
        _TOKEN_CACHE.pop(self._cache_key(), None)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict | None = None,
    ) -> tuple[bool, Any, int]:
        client = await self._get_client()
        headers = await self._headers()
        for attempt in range(2):
            try:
                resp = await client.request(
                    method, path, headers=headers, json=json, params=params
                )
            except Exception as exc:  # noqa: BLE001
                return False, str(exc).strip() or exc.__class__.__name__, 0
            if resp.status_code in (401, 403) and attempt == 0 and self._auth_mode == "bearer":
                self._invalidate_token()
                headers = await self._headers()
                continue
            ok, data = ok_json(resp)
            return ok, data, resp.status_code
        return False, "auth failed", 401

    # ── catalog ──────────────────────────────────────────────

    async def list_services(self) -> list[dict[str, Any]]:
        if self._services_cache is not None:
            return self._services_cache
        ok, data, code = await self._request(
            "GET", "/api/services", params={"page": 1, "limit": 100}
        )
        if not ok:
            raise RuntimeError(err_msg(data, f"services HTTP {code}"))
        items: list = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            raw = data.get("items") or data.get("services") or []
            if isinstance(raw, list):
                items = raw
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
            out.append(
                {
                    "id": sid,
                    "name": str(item.get("name") or f"service-{sid}"),
                    "is_disabled": False,
                }
            )
        self._services_cache = out
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
                    "inbound_count": 1,
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
                    "is_disabled": bool(svc.get("is_disabled")),
                }
            )
        return out

    def _selected_service_ids(self, payload: UserPayload) -> list[int]:
        ids: list[int] = []
        seen: set[int] = set()

        def add(raw: Any) -> None:
            try:
                n = int(raw)
            except (TypeError, ValueError):
                return
            if n > 0 and n not in seen:
                seen.add(n)
                ids.append(n)

        for key in ("service_ids", "services"):
            arr = payload.config.get(key) or self.meta.get(key)
            if isinstance(arr, list):
                for x in arr:
                    add(x)
        add(payload.config.get("service_id") or self.meta.get("service_id"))

        protocols = payload.config.get("protocols")
        if isinstance(protocols, list):
            for p in protocols:
                s = str(p).strip().lower()
                if s.startswith("service-"):
                    add(s.split("-", 1)[1])
        return ids

    async def _resolve_service_ids(self, payload: UserPayload) -> list[int]:
        selected = self._selected_service_ids(payload)
        try:
            catalog = await self.list_services()
        except Exception as exc:
            if selected:
                return selected
            raise RuntimeError(f"لیست سرویس‌های گارد دریافت نشد: {exc}") from exc
        available = [int(s["id"]) for s in catalog]
        if selected:
            if available:
                matched = [i for i in selected if i in available]
                if matched:
                    return matched
                raise RuntimeError(f"سرویس‌های انتخاب‌شده در گارد پیدا نشد: {selected}")
            return selected
        if available:
            return [available[0]]
        raise RuntimeError("در پنل گارد هیچ سرویسی تعریف نشده")

    def _want_disabled(self, payload: UserPayload) -> bool:
        return (payload.status or "").lower() in ("limited", "expired", "disabled")

    def _hold_days(self, payload: UserPayload) -> int | None:
        st = (payload.status or "").lower()
        if st != "on_hold":
            return None
        raw = (payload.config or {}).get("on_hold_timeout_days") or (
            payload.config or {}
        ).get("on_hold_expire_duration")
        try:
            if raw is None:
                return 30
            n = int(raw)
            # if duration looks like seconds (Rebecca style), convert to days
            if n > 365:
                n = max(1, n // 86400)
            return max(1, min(365, n))
        except (TypeError, ValueError):
            return 30

    def _build_create_body(self, payload: UserPayload, services: list[int]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "username": payload.username,
            "note": payload.note or "",
            "services": services,
        }
        data_limit = int(payload.data_limit or 0)
        if data_limit > 0:
            body["limit_usage"] = data_limit
        hold = self._hold_days(payload)
        if hold:
            body["on_hold_timeout_days"] = hold
            # on-hold: no fixed expire until first connect
        else:
            exp = int(payload.expire or 0) if payload.expire else 0
            if exp > 0:
                body["limit_expire"] = exp
        return body

    def _build_update_body(self, payload: UserPayload, services: list[int]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "usernames": [payload.username],
            "note": payload.note or "",
            "services": services,
            "enabled": not self._want_disabled(payload),
        }
        data_limit = int(payload.data_limit or 0)
        body["limit_usage"] = data_limit if data_limit > 0 else 0
        hold = self._hold_days(payload)
        if hold:
            body["on_hold_timeout_days"] = hold
            body["limit_expire"] = 0
        else:
            exp = int(payload.expire or 0) if payload.expire else 0
            body["limit_expire"] = exp if exp > 0 else 0
        return body

    def _derive_status(self, data: dict) -> str:
        if data.get("enabled") is False:
            return "disabled"
        if data.get("limited_at"):
            return "limited"
        if data.get("expired_at"):
            return "expired"
        if data.get("activated") is False:
            return "on_hold"
        return "active"

    def _from_sub(self, data: dict, fallback_username: str) -> AdapterResult:
        uname = str(data.get("username") or fallback_username)
        rid = data.get("id")
        used = int(data.get("total_usage") or data.get("current_usage") or 0)
        links: list[str] = []
        sub_link = data.get("subscription_link")
        if sub_link:
            links.append(str(sub_link))
        result = AdapterResult(
            success=True,
            remote_username=uname,
            remote_id=str(rid) if rid is not None else uname,
            used_traffic=used,
            links=links,
            raw=data,
            remote_status=self._derive_status(data),
            remote_expire=coerce_unix_ts(data.get("limit_expire")) or None,
            online_at=coerce_unix_ts(data.get("last_online_at") or data.get("first_online_at"))
            or None,
        )
        return result

    def _pick_sub(self, data: Any, username: str) -> dict | None:
        if isinstance(data, dict) and data.get("username"):
            return data
        items: list = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            raw = data.get("items") or data.get("subscriptions") or []
            if isinstance(raw, list):
                items = raw
        uname = username.lower()
        for item in items:
            if isinstance(item, dict) and str(item.get("username") or "").lower() == uname:
                return item
        if len(items) == 1 and isinstance(items[0], dict):
            return items[0]
        return None

    # ── required API ─────────────────────────────────────────

    async def test_connection(self) -> AdapterResult:
        try:
            ok, data, code = await self._request("GET", "/api/admins/current")
            if ok and isinstance(data, dict):
                return AdapterResult(
                    success=True,
                    remote_username=str(data.get("username") or self.username or "ok"),
                    raw=data,
                )
            return AdapterResult(success=False, error=err_msg(data, f"HTTP {code}"))
        except Exception as e:  # noqa: BLE001
            return AdapterResult(success=False, error=str(e).strip() or e.__class__.__name__)

    async def get_usage(self, username: str, remote_id: str | None = None) -> AdapterResult:
        uname = (username or remote_id or "").strip()
        if not uname:
            return AdapterResult(success=False, error="username خالی است")
        # Prefer exact path if available; fall back to filtered list
        for path, params in (
            (f"/api/subscriptions/{uname}", None),
            ("/api/subscriptions", {"usernames": uname, "limit": 10, "page": 1}),
            ("/api/subscriptions", {"search": uname, "limit": 20, "page": 1}),
        ):
            ok, data, code = await self._request("GET", path, params=params)
            if not ok:
                if code == 404:
                    continue
                if path.endswith(f"/{uname}") and code in (400, 404, 405):
                    continue
                continue
            row = self._pick_sub(data, uname)
            if row:
                result = self._from_sub(row, uname)
                try:
                    client = await self._get_client()
                    result.links = await expand_subscription_links(
                        client, list(result.links or [])
                    )
                except Exception:
                    pass
                # Enrich with personalized config links
                try:
                    lok, ldata, _ = await self._request(
                        "GET", f"/api/subscriptions/{uname}/links"
                    )
                    if lok and isinstance(ldata, dict):
                        extra: list[str] = []
                        for item in ldata.get("links") or []:
                            if isinstance(item, dict) and item.get("link"):
                                extra.append(str(item["link"]))
                            elif isinstance(item, str):
                                extra.append(item)
                        merged = list(result.links or [])
                        for l in extra:
                            if l and l not in merged:
                                merged.append(l)
                        result.links = merged
                except Exception:
                    pass
                return result
        return AdapterResult(success=False, error=f"کاربر «{uname}» در گارد پیدا نشد")

    async def get_subscription_links(
        self, username: str, remote_id: str | None = None
    ) -> AdapterResult:
        return await self.get_usage(username, remote_id)

    async def create_user(self, payload: UserPayload) -> AdapterResult:
        try:
            existing = await self.get_usage(payload.username)
            if existing.success:
                return await self.modify_user(payload)

            services = await self._resolve_service_ids(payload)
            body = self._build_create_body(payload, services)
            # API accepts single object or array
            ok, data, code = await self._request("POST", "/api/subscriptions", json=[body])
            if not ok:
                # retry as single object
                ok, data, code = await self._request("POST", "/api/subscriptions", json=body)
            if not ok:
                err = err_msg(data, f"create HTTP {code}")
                low = err.lower()
                if any(x in low for x in ("already", "exist", "duplicate", "تکرار", "موجود")):
                    return await self.modify_user(payload)
                return AdapterResult(success=False, error=err)

            row = None
            if isinstance(data, list) and data:
                row = data[0] if isinstance(data[0], dict) else None
            elif isinstance(data, dict):
                row = self._pick_sub(data, payload.username) or data
            if not isinstance(row, dict):
                # fetch after create
                got = await self.get_usage(payload.username)
                if got.success:
                    row = got.raw if isinstance(got.raw, dict) else None
                    if row is None:
                        if self._want_disabled(payload):
                            await self._set_enabled(payload.username, False)
                        return got
                return AdapterResult(success=False, error="پاسخ ساخت کاربر نامعتبر بود")

            result = self._from_sub(row, payload.username)
            if self._want_disabled(payload):
                dis = await self._set_enabled(payload.username, False)
                if dis.success and isinstance(dis.raw, dict):
                    result = self._from_sub(dis.raw, payload.username)
            try:
                client = await self._get_client()
                result.links = await expand_subscription_links(
                    client, list(result.links or [])
                )
            except Exception:
                pass
            # Prefer full links fetch
            linked = await self.get_usage(payload.username)
            if linked.success and linked.links:
                result.links = linked.links
                result.used_traffic = linked.used_traffic or result.used_traffic
            return result
        except Exception as e:  # noqa: BLE001
            return AdapterResult(success=False, error=str(e).strip() or e.__class__.__name__)

    async def modify_user(self, payload: UserPayload) -> AdapterResult:
        try:
            services = await self._resolve_service_ids(payload)
            body = self._build_update_body(payload, services)
            ok, data, code = await self._request("PUT", "/api/subscriptions", json=body)
            if not ok:
                # older single-user path
                single = {k: v for k, v in body.items() if k != "usernames"}
                ok, data, code = await self._request(
                    "PUT", f"/api/subscriptions/{payload.username}", json=single
                )
            if not ok:
                return AdapterResult(success=False, error=err_msg(data, f"modify HTTP {code}"))

            row = None
            if isinstance(data, list) and data:
                row = data[0] if isinstance(data[0], dict) else None
            elif isinstance(data, dict):
                row = self._pick_sub(data, payload.username) or data

            # Explicit enable/disable for quota freeze accuracy
            want_off = self._want_disabled(payload)
            dis = await self._set_enabled(payload.username, not want_off)
            if dis.success and isinstance(dis.raw, dict):
                row = dis.raw

            if isinstance(row, dict):
                result = self._from_sub(row, payload.username)
            else:
                result = await self.get_usage(payload.username)
                if not result.success:
                    return result
            try:
                client = await self._get_client()
                result.links = await expand_subscription_links(
                    client, list(result.links or [])
                )
            except Exception:
                pass
            return result
        except Exception as e:  # noqa: BLE001
            return AdapterResult(success=False, error=str(e).strip() or e.__class__.__name__)

    async def _set_enabled(self, username: str, enabled: bool) -> AdapterResult:
        path = "/api/subscriptions/enable" if enabled else "/api/subscriptions/disable"
        ok, data, code = await self._request(
            "POST", path, json={"usernames": [username]}
        )
        if not ok:
            return AdapterResult(success=False, error=err_msg(data, f"enable/disable HTTP {code}"))
        row = None
        if isinstance(data, list) and data and isinstance(data[0], dict):
            row = data[0]
        elif isinstance(data, dict):
            row = data
        if isinstance(row, dict):
            return self._from_sub(row, username)
        return AdapterResult(success=True, remote_username=username, raw=data)

    async def delete_user(self, username: str, remote_id: str | None = None) -> AdapterResult:
        uname = (username or remote_id or "").strip()
        if not uname:
            return AdapterResult(success=False, error="username خالی است")
        ok, data, code = await self._request(
            "DELETE", "/api/subscriptions", json={"usernames": [uname]}
        )
        if not ok:
            ok2, data2, code2 = await self._request(
                "DELETE", f"/api/subscriptions/{uname}"
            )
            if not ok2 and code2 not in (404,):
                return AdapterResult(
                    success=False, error=err_msg(data2, err_msg(data, f"delete HTTP {code}"))
                )
        return AdapterResult(success=True, remote_username=uname)

    async def reset_usage(self, username: str, remote_id: str | None = None) -> AdapterResult:
        uname = (username or remote_id or "").strip()
        if not uname:
            return AdapterResult(success=False, error="username خالی است")
        ok, data, code = await self._request(
            "POST", "/api/subscriptions/reset", json={"usernames": [uname]}
        )
        if not ok:
            return AdapterResult(success=False, error=err_msg(data, f"reset HTTP {code}"))
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return self._from_sub(data[0], uname)
        return AdapterResult(success=True, remote_username=uname, used_traffic=0, raw=data)
