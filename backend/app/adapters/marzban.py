"""Marzban panel adapter — auth via username/password → POST /api/admin/token.

User create/modify follows official Marzban docs:
- username: 3–32 chars, a-z 0-9 underscore
- proxies: { protocol: settings }
- inbounds: { protocol: [inbound_tags] } — empty/missing tags = all inbounds for that protocol
"""

from __future__ import annotations

import asyncio
import time
import uuid as uuid_lib
from typing import Any

import httpx

from app.adapters.base import AdapterResult, BasePanelAdapter, UserPayload
from app.adapters.http_utils import err_msg, make_client, ok_json

# Protocols Marzban/Rebecca accept in proxies/inbounds
KNOWN_PROTOCOLS = ("vless", "vmess", "trojan", "shadowsocks")

# Shared across adapter instances (batch refresh / multi-user ops)
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_INBOUNDS_CACHE: dict[str, tuple[dict[str, list[str]], float]] = {}
_TOKEN_TTL = 240.0  # seconds
_INBOUNDS_TTL = 120.0


class MarzbanAdapter(BasePanelAdapter):
    """Official Marzban REST API (also base for Rebecca)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._token: str | None = None
        self._client = None
        self._inbounds_cache: dict[str, list[str]] | None = None

    def _cache_key(self) -> str:
        return f"{self.base_url}|{(self.username or '').strip()}"

    async def _get_client(self):
        if self._client is None:
            # No X-Requested-With — Marzban/PasarGuard token endpoint is OAuth2 form, not 3x-ui
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

    async def _post_token(self, client, form: dict[str, str]):
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        return await client.post("/api/admin/token", data=form, headers=headers)

    async def _ensure_token(self) -> str:
        if self._token:
            return self._token
        user = (self.username or "").strip()
        password = (self.password or "").strip()
        if not user or not password:
            raise RuntimeError("Marzban/Rebecca/PasarGuard نیاز به نام کاربری و رمز عبور دارند")
        self.username = user
        self.password = password

        cache_key = self._cache_key()
        cached = _TOKEN_CACHE.get(cache_key)
        if cached and time.monotonic() < cached[1]:
            self._token = cached[0]
            return self._token

        # Official OAuth2 password grant; some forks reject unknown fields — try variants
        attempts: list[dict[str, str]] = [
            {"grant_type": "password", "username": user, "password": password},
            {"grant_type": "password", "username": user, "password": password, "scope": ""},
            {"username": user, "password": password},
        ]

        client = await self._get_client()
        resp = None
        ok = False
        data: Any = None
        last_detail = "Login failed"

        for form in attempts:
            for net_try in range(4):
                try:
                    resp = await self._post_token(client, form)
                    break
                except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as exc:
                    last_detail = f"شبکه پنل: {exc.__class__.__name__}"
                    if net_try < 3:
                        await self.close()
                        await asyncio.sleep(0.25 + net_try * 0.2)
                        client = await self._get_client()
                        continue
                    raise RuntimeError(last_detail) from exc
            if resp is None:
                continue
            ok, data = ok_json(resp)
            if ok and isinstance(data, dict) and data.get("access_token"):
                break
            last_detail = err_msg(data, f"Login failed HTTP {resp.status_code}")
            # Wrong password — no point trying other form shapes against same URL
            if resp.status_code == 401:
                break

        if (not ok or not isinstance(data, dict) or not data.get("access_token")) and resp is not None and resp.status_code in (
            404,
            405,
        ):
            from app.adapters.http_utils import normalize_panel_base_url

            root = normalize_panel_base_url(self.base_url)
            retried = False
            if root != self.base_url:
                self.base_url = root
                retried = True
            elif "/dashboard" in self.base_url.lower():
                self.base_url = normalize_panel_base_url(
                    self.base_url.lower().replace("/dashboard", "")
                )
                retried = True
            if retried:
                await self.close()
                client = await self._get_client()
                for form in attempts:
                    resp = await self._post_token(client, form)
                    ok, data = ok_json(resp)
                    if ok and isinstance(data, dict) and data.get("access_token"):
                        break
                    last_detail = err_msg(data, f"Login failed HTTP {resp.status_code}")
                    if resp.status_code == 401:
                        break

        if not ok or not isinstance(data, dict) or not data.get("access_token"):
            detail = last_detail
            if resp is not None and resp.status_code in (404, 405):
                detail += (
                    " — آدرس پایه را بدون /dashboard وارد کنید "
                    f"(فعلی: {self.base_url})"
                )
            raise RuntimeError(detail)
        self._token = data["access_token"]
        _TOKEN_CACHE[cache_key] = (self._token, time.monotonic() + _TOKEN_TTL)
        return self._token

    async def _headers(self) -> dict[str, str]:
        token = await self._ensure_token()
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    async def _authed(self) -> tuple[Any, dict[str, str]]:
        """Token first, then a live client.

        Critical: never reuse a client reference taken *before* `_ensure_token`,
        because token retries may `close()` that client →
        «Cannot send a request, as the client has been closed».
        """
        headers = await self._headers()
        client = await self._get_client()
        return client, headers

    def _selected_protocols(self, payload: UserPayload) -> list[str]:
        """Resolve which protocols to enable from group/panel config (docs order)."""
        raw = payload.config.get("protocols")
        if isinstance(raw, list) and raw:
            selected = [str(x).lower().strip() for x in raw if x]
        else:
            inbounds = payload.config.get("inbounds")
            if isinstance(inbounds, dict) and inbounds:
                selected = [str(k).lower().strip() for k in inbounds.keys()]
            else:
                meta_proto = self.meta.get("protocols")
                if isinstance(meta_proto, list) and meta_proto:
                    selected = [str(x).lower().strip() for x in meta_proto if x]
                else:
                    meta_ib = self.meta.get("inbounds")
                    if isinstance(meta_ib, dict) and meta_ib:
                        selected = [str(k).lower().strip() for k in meta_ib.keys()]
                    else:
                        selected = list(KNOWN_PROTOCOLS)

        out: list[str] = []
        for p in selected:
            if p in KNOWN_PROTOCOLS and p not in out:
                out.append(p)
        return out or ["vless"]

    def _proxy_settings_for(self, protocol: str, payload: UserPayload, uid: str) -> dict[str, Any]:
        """Build per-protocol settings. Marzban fills defaults for empty objects where allowed."""
        explicit = payload.config.get("proxies")
        if isinstance(explicit, dict) and isinstance(explicit.get(protocol), dict):
            settings = dict(explicit[protocol])
            # Ensure vless/vmess always have id when partial settings provided
            if protocol in ("vless", "vmess") and not settings.get("id"):
                settings["id"] = uid
            if protocol == "vless" and "flow" not in settings:
                settings["flow"] = payload.config.get("flow", "") or ""
            return settings

        if protocol == "vless":
            return {"id": uid, "flow": payload.config.get("flow", "") or ""}
        if protocol == "vmess":
            return {"id": uid}
        if protocol == "trojan":
            return {}
        if protocol == "shadowsocks":
            # Empty → Marzban generates password; method optional (panel default)
            ss: dict[str, Any] = {}
            method = payload.config.get("shadowsocks_method") or self.meta.get("shadowsocks_method")
            if method:
                ss["method"] = str(method)
            return ss
        return {}

    def _build_proxies(self, payload: UserPayload, protocols: list[str]) -> dict[str, Any]:
        uid = payload.uuid or str(uuid_lib.uuid4())
        return {p: self._proxy_settings_for(p, payload, uid) for p in protocols}

    def _build_inbounds(
        self,
        payload: UserPayload,
        protocols: list[str],
        available: dict[str, list[str]] | None = None,
    ) -> dict[str, list[str]]:
        """
        Build inbounds map per docs:
        - if tags provided for a protocol → use those (intersect with available if known)
        - if empty/missing → all available tags for that protocol (or [] so panel assigns all)
        """
        configured = payload.config.get("inbounds")
        if not isinstance(configured, dict):
            configured = self.meta.get("inbounds") if isinstance(self.meta.get("inbounds"), dict) else {}

        out: dict[str, list[str]] = {}
        for protocol in protocols:
            raw_tags = configured.get(protocol)
            tags: list[str] = []
            if isinstance(raw_tags, list):
                tags = [str(t) for t in raw_tags if t]
            elif isinstance(raw_tags, str) and raw_tags.strip():
                tags = [raw_tags.strip()]

            if available and protocol in available:
                avail = available[protocol]
                if tags:
                    tags = [t for t in tags if t in avail]
                if not tags:
                    tags = list(avail)

            out[protocol] = tags
        return out

    async def _available_inbounds_map(self) -> dict[str, list[str]]:
        if self._inbounds_cache is not None:
            return self._inbounds_cache
        key = self._cache_key()
        shared = _INBOUNDS_CACHE.get(key)
        if shared and time.monotonic() < shared[1]:
            self._inbounds_cache = shared[0]
            return self._inbounds_cache
        try:
            rows = await asyncio.wait_for(self.list_protocols(), timeout=18)
            cache = {str(r["protocol"]).lower(): list(r.get("tags") or []) for r in rows}
            self._inbounds_cache = cache
            _INBOUNDS_CACHE[key] = (cache, time.monotonic() + _INBOUNDS_TTL)
            return cache
        except Exception:
            return {}

    def _config_has_inbound_tags(self, payload: UserPayload, protocols: list[str]) -> bool:
        """True when every protocol already has explicit inbound tags (skip catalog GET)."""
        configured = payload.config.get("inbounds")
        if not isinstance(configured, dict):
            configured = self.meta.get("inbounds") if isinstance(self.meta.get("inbounds"), dict) else {}
        if not isinstance(configured, dict) or not protocols:
            return False
        for protocol in protocols:
            raw = configured.get(protocol)
            if isinstance(raw, list) and any(str(t).strip() for t in raw):
                continue
            if isinstance(raw, str) and raw.strip():
                continue
            return False
        return True

    async def _build_user_body(self, payload: UserPayload, *, include_username: bool) -> dict[str, Any]:
        protocols = self._selected_protocols(payload)
        # Skip GET /api/inbounds when group/panel already pinned inbound tags
        if self._config_has_inbound_tags(payload, protocols):
            available: dict[str, list[str]] = {}
        else:
            available = await self._available_inbounds_map()

        # Drop protocols that are not enabled on the panel (avoid HTTP 400)
        if available:
            enabled = [p for p in protocols if available.get(p)]
            if enabled:
                protocols = enabled
            # if catalog empty/failed, keep requested protocols and let panel validate

        proxies = self._build_proxies(payload, protocols)
        inbounds = self._build_inbounds(payload, protocols, available or None)

        # Create API only allows active|on_hold; modify allows disabled|limited|expired too
        if include_username:
            status = payload.status if payload.status in ("active", "on_hold") else "active"
        else:
            _ok = ("active", "on_hold", "disabled", "limited", "expired")
            status = payload.status if payload.status in _ok else "disabled"
        body: dict[str, Any] = {
            "proxies": proxies,
            "inbounds": inbounds,
            "data_limit": int(payload.data_limit or 0),
            "data_limit_reset_strategy": payload.config.get("data_limit_reset_strategy", "no_reset"),
            "status": status,
            "note": payload.note or "",
            "expire": int(payload.expire or 0),
        }
        if include_username:
            body["username"] = payload.username
        # Official Marzban schema has no limit_ip — do not send it (causes 400/422)
        return body

    @staticmethod
    def _parse_used_traffic(data: dict) -> int:
        """Best-effort used bytes from Marzban / PasarGuard / forks."""
        vals: list[int] = []
        for key in ("used_traffic", "lifetime_used_traffic", "total_traffic"):
            raw = data.get(key)
            if raw is None or raw == "":
                continue
            try:
                vals.append(max(0, int(raw)))
            except (TypeError, ValueError):
                continue
        traffic = data.get("traffic")
        if isinstance(traffic, dict):
            for key in ("used", "used_traffic", "download", "down", "uplink", "download_bytes"):
                raw = traffic.get(key)
                if raw is None or raw == "":
                    continue
                try:
                    vals.append(max(0, int(raw)))
                except (TypeError, ValueError):
                    continue
            try:
                up = int(traffic.get("upload") or traffic.get("up") or 0)
                down = int(traffic.get("download") or traffic.get("down") or 0)
                if up or down:
                    vals.append(max(0, up + down))
            except (TypeError, ValueError):
                pass
        return max(vals) if vals else 0

    def _from_user_response(self, data: dict, fallback_username: str) -> AdapterResult:
        proxies = data.get("proxies") or {}
        uuid = None
        if isinstance(proxies, dict):
            for proto in ("vless", "vmess"):
                if isinstance(proxies.get(proto), dict) and proxies[proto].get("id"):
                    uuid = proxies[proto]["id"]
                    break
        links = [str(x) for x in (data.get("links") or []) if x]
        sub = data.get("subscription_url")
        if sub and str(sub) not in links:
            links.append(str(sub))
        return AdapterResult(
            success=True,
            remote_username=data.get("username", fallback_username),
            remote_id=data.get("username", fallback_username),
            uuid=uuid,
            links=links,
            used_traffic=self._parse_used_traffic(data),
            raw=data,
        )

    async def _expand_subscription_links(self, links: list[str]) -> list[str]:
        """Resolve /sub/... or https sub URLs into vless/vmess share links."""
        from app.adapters.http_utils import expand_subscription_links

        client = await self._get_client()
        return await expand_subscription_links(client, list(links or []))

    async def list_protocols(self) -> list[dict[str, Any]]:
        """
        Fetch Marzban/Rebecca protocols from GET /api/inbounds.
        Response shape: { "vless": [ProxyInbound...], "vmess": [...], ... }
        """
        client, headers = await self._authed()
        resp = await client.get(
            "/api/inbounds",
            headers=headers,
            timeout=httpx.Timeout(18.0, connect=12.0),
        )
        ok, data = ok_json(resp)
        if not ok or not isinstance(data, dict):
            raise RuntimeError(err_msg(data, f"HTTP {resp.status_code}"))
        out: list[dict[str, Any]] = []
        for protocol, items in data.items():
            if not isinstance(items, list):
                continue
            tags: list[str] = []
            details: list[dict[str, Any]] = []
            for item in items:
                if isinstance(item, dict) and item.get("tag"):
                    tag = str(item["tag"])
                    tags.append(tag)
                    details.append(
                        {
                            "tag": tag,
                            "protocol": str(item.get("protocol") or protocol),
                            "network": str(item.get("network") or ""),
                            "tls": str(item.get("tls") or ""),
                            "port": item.get("port"),
                        }
                    )
                elif isinstance(item, str) and item.strip():
                    tag = item.strip()
                    tags.append(tag)
                    details.append(
                        {
                            "tag": tag,
                            "protocol": str(protocol),
                            "network": "",
                            "tls": "",
                            "port": None,
                        }
                    )
            out.append(
                {
                    "protocol": str(protocol),
                    "tags": tags,
                    "inbound_count": len(tags),
                    "inbounds": details,
                }
            )
        return out

    async def list_inbounds(self) -> list[dict[str, Any]]:
        """Flat inbound tags (legacy helper). Prefer list_protocols()."""
        flat: list[dict[str, Any]] = []
        for proto in await self.list_protocols():
            flat.extend(proto.get("inbounds") or [])
        return flat

    async def test_connection(self) -> AdapterResult:
        try:
            client, headers = await self._authed()
            resp = await client.get("/api/admin", headers=headers)
            ok, data = ok_json(resp)
            if ok:
                return AdapterResult(success=True, raw=data)
            return AdapterResult(success=False, error=err_msg(data, f"HTTP {resp.status_code}"))
        except Exception as e:
            msg = str(e).strip() or e.__class__.__name__
            return AdapterResult(success=False, error=msg)

    async def _finalize_user_result(
        self, result: AdapterResult, username: str, *, expand: bool = False
    ) -> AdapterResult:
        """Optionally expand /sub → share links. Default off for fast create/modify."""
        if not expand:
            return result
        from app.adapters.http_utils import is_share_link, is_subscription_ref

        links = [str(x).strip() for x in (result.links or []) if x and str(x).strip()]
        if any(is_share_link(l) for l in links):
            result.links = [l for l in links if is_share_link(l)]
            return result
        if not any(is_subscription_ref(l) for l in links):
            return result
        try:
            expanded = await asyncio.wait_for(
                self._expand_subscription_links(links), timeout=15
            )
            if expanded:
                result.links = expanded
        except Exception:
            pass
        return result

    async def _fetch_existing_user(self, username: str) -> AdapterResult | None:
        """Fast GET /api/user (no /sub expand) — prove the user exists on Marzban."""
        for i in range(8):
            try:
                existing = await asyncio.wait_for(
                    self.get_usage(username, expand_links=False), timeout=10
                )
                if existing.success:
                    # Best-effort expand; never fail existence because /sub is slow
                    return await self._finalize_user_result(
                        existing, username, expand=True
                    )
            except Exception:
                pass
            self._token = None
            _TOKEN_CACHE.pop(self._cache_key(), None)
            await self.close()
            await asyncio.sleep(0.2 + i * 0.15)
        return None

    async def create_user(self, payload: UserPayload) -> AdapterResult:
        """POST /api/user — fast path + confirm-by-GET on Marzban Cloud timeouts.

        Strategy:
        1) POST once (short timeouts)
        2) On success → return (optional link expand)
        3) On timeout/network → GET user; if found → success (no manual retry needed)
        4) Retry a few more POSTs only if GET says not found
        """
        body = await self._build_user_body(payload, include_username=True)
        last_err = "create failed"
        req_timeout = httpx.Timeout(12.0, connect=4.0)

        for attempt in range(5):
            try:
                client, headers = await self._authed()
                resp = await client.post(
                    "/api/user",
                    headers=headers,
                    json=body,
                    timeout=req_timeout,
                )
                ok, data = ok_json(resp)
                if ok and isinstance(data, dict):
                    result = self._from_user_response(data, payload.username)
                    return await self._finalize_user_result(
                        result, payload.username, expand=True
                    )

                err = err_msg(data, f"HTTP {resp.status_code}")
                low = err.lower()
                if any(x in low for x in ("exist", "already", "duplicate", "تکرار", "موجود")):
                    existing = await self._fetch_existing_user(payload.username)
                    if existing:
                        return existing
                    # Exists but GET flaky — still OK for Astra (retry UI not needed)
                    return AdapterResult(
                        success=True,
                        remote_username=payload.username,
                        remote_id=payload.username,
                        links=[],
                        used_traffic=0,
                    )

                last_err = err
                if resp.status_code in (401, 403) and attempt < 4:
                    self._token = None
                    _TOKEN_CACHE.pop(self._cache_key(), None)
                    await self.close()
                    continue
                # Hard API rejection — still check exists (race)
                existing = await self._fetch_existing_user(payload.username)
                if existing:
                    return existing
                return AdapterResult(success=False, error=err)
            except Exception as e:
                last_err = str(e).strip() or e.__class__.__name__
                if isinstance(e, (asyncio.TimeoutError, httpx.TimeoutException)):
                    last_err = "Timeout waiting for panel response"
                if "client has been closed" in last_err.lower():
                    last_err = "اتصال پنل قطع شد — دوباره تلاش کنید"

                # Most common Marzban Cloud case: POST accepted, response lost
                existing = await self._fetch_existing_user(payload.username)
                if existing:
                    return existing

                if attempt < 4:
                    self._token = None
                    _TOKEN_CACHE.pop(self._cache_key(), None)
                    await self.close()
                    await asyncio.sleep(0.2 + attempt * 0.15)
                    continue

        existing = await self._fetch_existing_user(payload.username)
        if existing:
            return existing
        return AdapterResult(success=False, error=last_err)

    async def modify_user(self, payload: UserPayload) -> AdapterResult:
        """PUT /api/user/{username} — official Marzban modify."""
        try:
            body = await self._build_user_body(payload, include_username=False)
            client, headers = await self._authed()
            resp = await client.put(
                f"/api/user/{payload.username}",
                headers=headers,
                json=body,
                timeout=httpx.Timeout(30.0, connect=15.0),
            )
            ok, data = ok_json(resp)
            if ok and isinstance(data, dict):
                return self._from_user_response(data, payload.username)
            return AdapterResult(success=False, error=err_msg(data, f"HTTP {resp.status_code}"))
        except Exception as e:
            msg = str(e).strip() or e.__class__.__name__
            if isinstance(e, (asyncio.TimeoutError, httpx.TimeoutException)):
                msg = "Timeout waiting for panel response"
            if "client has been closed" in msg.lower():
                msg = "اتصال پنل قطع شد — دوباره تلاش کنید"
            return AdapterResult(success=False, error=msg)

    async def delete_user(self, username: str, remote_id: str | None = None) -> AdapterResult:
        """DELETE /api/user/{username} — Marzban/PasarGuard docs; verify gone via GET."""
        from urllib.parse import quote

        uname = (username or remote_id or "").strip()
        if not uname:
            return AdapterResult(success=False, error="username خالی است")
        path = f"/api/user/{quote(uname, safe='')}"
        last_err = "delete failed"
        t = httpx.Timeout(20.0, connect=5.0)
        for attempt in range(6):
            try:
                client, headers = await self._authed()
                resp = await client.delete(path, headers=headers, timeout=t)
                if resp.status_code not in (200, 204, 404):
                    ok, data = ok_json(resp)
                    err = err_msg(data, f"HTTP {resp.status_code}")
                    if "not found" in err.lower():
                        return AdapterResult(success=True)
                    last_err = err
                    if resp.status_code in (401, 403) and attempt < 5:
                        self._token = None
                        _TOKEN_CACHE.pop(self._cache_key(), None)
                        await self.close()
                        continue
                    return AdapterResult(success=False, error=err)

                # Confirm removed (or never existed). If still 200 → delete lied/failed.
                client, headers = await self._authed()
                check = await client.get(path, headers=headers, timeout=t)
                if check.status_code == 404:
                    return AdapterResult(success=True)
                if check.status_code in (200, 201):
                    last_err = "کاربر هنوز روی پنل اصلی وجود دارد"
                    if attempt < 5:
                        await asyncio.sleep(0.3)
                        continue
                    return AdapterResult(success=False, error=last_err)
                # Other codes after DELETE → treat as gone
                return AdapterResult(success=True)
            except Exception as e:
                last_err = str(e).strip() or e.__class__.__name__
                if "client has been closed" in last_err.lower() and attempt < 5:
                    self._token = None
                    await self.close()
                    await asyncio.sleep(0.25)
                    continue
                if isinstance(e, (asyncio.TimeoutError, httpx.TimeoutException)):
                    last_err = "Timeout waiting for panel response"
                if attempt < 5:
                    await self.close()
                    await asyncio.sleep(0.25 + attempt * 0.15)
                    continue
                return AdapterResult(success=False, error=last_err)
        return AdapterResult(success=False, error=last_err)

    async def reset_usage(self, username: str, remote_id: str | None = None) -> AdapterResult:
        """POST /api/user/{username}/reset — Marzban / PasarGuard / Rebecca family."""
        try:
            client, headers = await self._authed()
            resp = await client.post(
                f"/api/user/{username}/reset",
                headers=headers,
                timeout=httpx.Timeout(25.0, connect=15.0),
            )
            ok, data = ok_json(resp)
            if ok:
                if isinstance(data, dict):
                    return self._from_user_response(data, username)
                return AdapterResult(success=True, remote_username=username, used_traffic=0)
            if resp.status_code in (404, 405):
                return AdapterResult(success=False, error=err_msg(data, "reset endpoint missing"))
            return AdapterResult(success=False, error=err_msg(data, f"HTTP {resp.status_code}"))
        except Exception as e:
            return AdapterResult(success=False, error=str(e).strip() or e.__class__.__name__)

    async def get_usage(
        self,
        username: str,
        remote_id: str | None = None,
        *,
        expand_links: bool = True,
    ) -> AdapterResult:
        try:
            client, headers = await self._authed()
            resp = await client.get(
                f"/api/user/{username}",
                headers=headers,
                timeout=httpx.Timeout(20.0, connect=12.0),
            )
            ok, data = ok_json(resp)
            if ok and isinstance(data, dict):
                result = self._from_user_response(data, username)
                if expand_links:
                    result.links = await self._expand_subscription_links(list(result.links or []))
                return result
            return AdapterResult(success=False, error=err_msg(data, f"HTTP {resp.status_code}"))
        except Exception as e:
            return AdapterResult(success=False, error=str(e).strip() or e.__class__.__name__)

    async def get_subscription_links(self, username: str, remote_id: str | None = None) -> AdapterResult:
        return await self.get_usage(username, remote_id, expand_links=True)
