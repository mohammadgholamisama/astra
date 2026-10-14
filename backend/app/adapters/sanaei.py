"""
3x-ui / Sanaei adapter — per official docs.sanaei.dev

Auth:
  Authorization: Bearer <API Token>   (Settings → Security → API Token)
  Optional session cookie via username/password as fallback.

Note: without X-Requested-With, invalid tokens often look like HTTP 404.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import uuid as uuid_lib
from typing import Any

import httpx

from app.adapters.base import AdapterResult, BasePanelAdapter, UserPayload
from app.adapters.http_utils import err_msg, make_client, ok_json

logger = logging.getLogger("astra.sanaei")


def _clean_token(raw: str | None) -> str:
    t = (raw or "").strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    return t


class SanaeiAdapter(BasePanelAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = None
        self._cookies = None
        self._csrf: str | None = None
        self._session_ok = False

    def _token(self) -> str:
        return _clean_token(self.api_token)

    def _require_token(self) -> str:
        token = self._token()
        if not token:
            raise RuntimeError(
                "برای سنایی (3x-ui) API Token الزامی است — از Settings → Security → API Token کپی کنید"
            )
        return token

    async def _get_client(self):
        if self._client is None:
            # Heavy inbound payloads behind Cloudflare often need a long read window
            self._client = make_client(
                self.base_url,
                self.meta.get("verify_ssl", False),
                timeout=180.0,
            )
        return self._client

    async def _curl_json(self, path: str, *, max_time: int = 120) -> tuple[bool, Any, int]:
        """
        Fallback fetch via curl — on some VPS/Cloudflare paths httpx ReadTimeouts
        while curl completes fine for the same URL.
        """
        token = self._token()
        if not token:
            return False, {"msg": "API Token لازم است"}, 0
        url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
        cmd = [
            "curl",
            "-sS",
            "-L",
            "--max-time",
            str(max_time),
            "-H",
            f"Authorization: Bearer {token}",
            "-H",
            "X-Requested-With: XMLHttpRequest",
            "-H",
            "Accept: application/json",
            "-w",
            "\n__HTTP_STATUS__:%{http_code}",
            url,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(), timeout=max_time + 15
            )
        except asyncio.TimeoutError:
            return False, {"msg": f"curl timeout بعد از {max_time}s"}, 0
        except FileNotFoundError:
            return False, {"msg": "curl روی سرور نصب نیست"}, 0

        raw = (out_b or b"").decode("utf-8", errors="replace")
        err = (err_b or b"").decode("utf-8", errors="replace")
        status = 0
        body = raw
        if "__HTTP_STATUS__:" in raw:
            body, _, tail = raw.rpartition("__HTTP_STATUS__:")
            try:
                status = int(tail.strip().splitlines()[0].strip())
            except ValueError:
                status = 0
            body = body.rstrip("\n")
        if proc.returncode not in (0, None) and status == 0:
            return False, {"msg": (err or raw or "curl failed")[:400]}, status
        try:
            data = json.loads(body) if body.strip() else {}
        except json.JSONDecodeError:
            return False, {"text": body[:300], "msg": "پاسخ JSON نبود"}, status or 200
        if status >= 400:
            return False, data if isinstance(data, dict) else {"data": data}, status
        if isinstance(data, dict) and data.get("success") is False:
            return False, data, status or 200
        return True, data, status or 200

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _auth_headers(self, *, with_bearer: bool = True) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        if with_bearer:
            headers["Authorization"] = f"Bearer {self._require_token()}"
        if self._csrf:
            headers["X-CSRF-Token"] = str(self._csrf)
        return headers

    async def _cookie_login(self) -> None:
        """Session login (username/password) — fallback when Bearer fails."""
        if not self.username or not self.password:
            raise RuntimeError("نام کاربری/رمز پنل برای لاگین سشن لازم است")
        client = await self._get_client()
        self._csrf = None

        for csrf_path in ("csrf-token", "getCSRFToken", "/csrf-token", "/getCSRFToken"):
            try:
                csrf_resp = await client.get(csrf_path.lstrip("/"))
                if csrf_resp.status_code == 200:
                    data = csrf_resp.json()
                    tok = data.get("obj") or data.get("csrf") or data.get("token") or ""
                    if isinstance(tok, dict):
                        tok = tok.get("token") or ""
                    if tok:
                        self._csrf = str(tok)
                        break
            except Exception:
                continue

        headers: dict[str, str] = {
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        if self._csrf:
            headers["X-CSRF-Token"] = str(self._csrf)

        body = {"username": self.username, "password": self.password}
        resp = await client.post("login", json=body, headers=headers)
        ok, data = ok_json(resp)
        if not ok:
            resp = await client.post("login", data=body, headers=headers)
            ok, data = ok_json(resp)
        self._cookies = resp.cookies
        if not ok:
            raise RuntimeError(err_msg(data, "لاگین سنایی ناموفق — یوزر/پسورد را چک کنید"))
        self._session_ok = True

    async def _request(self, method: str, path: str, **kwargs: Any):
        client = await self._get_client()
        path = path.lstrip("/")
        headers = {**self._auth_headers(with_bearer=bool(self._token())), **kwargs.pop("headers", {})}
        cookies = kwargs.pop("cookies", self._cookies)

        # Prefer Bearer when token exists
        if self._token():
            resp = await client.request(method, path, headers=headers, cookies=cookies, **kwargs)
            if resp.status_code in (200, 201) or (
                resp.status_code < 400 and resp.headers.get("content-type", "").startswith("application/json")
            ):
                # still may be success:false
                return resp
            if resp.status_code not in (401, 403, 404):
                return resp
            # 401/403/404 → try session if we have password
            if self.username and self.password:
                try:
                    await self._cookie_login()
                except Exception:
                    return resp
                headers = self._auth_headers(with_bearer=True)
                return await client.request(
                    method, path, headers=headers, cookies=self._cookies, **kwargs
                )
            return resp

        # Token-less: session only
        if not self._session_ok:
            await self._cookie_login()
        headers = self._auth_headers(with_bearer=False)
        return await client.request(method, path, headers=headers, cookies=self._cookies, **kwargs)

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
            "totalGB": int(payload.data_limit or 0),
            "expiryTime": expiry_ms,
            "enable": payload.status in ("active", "on_hold"),
            "tgId": tg_id,
            "limitIp": int(payload.config.get("limit_ip", 0) or 0),
            "flow": payload.config.get("flow", "") or "",
            "comment": payload.note or "",
        }

    def _explain_fail(self, resp, data: Any) -> str:
        code = getattr(resp, "status_code", None)
        base = self.base_url.rstrip("/")
        hint = err_msg(data, f"HTTP {code}")
        if code == 401:
            return (
                f"{hint} — API Token اشتباه/غیرفعال است یا یوزرپسورد غلط است. "
                "از Settings → Security → API Token یک توکن جدید بسازید."
            )
        if code == 403:
            return f"{hint} — توکن مجوز این مسیر را ندارد (scope)."
        if code == 404:
            return (
                f"مسیر API پیدا نشد (HTTP 404). آدرس پایه باید شامل webBasePath باشد "
                f"(مثلاً {base} و API می‌شود {base}/panel/api/...). "
                "اگر آدرس درست است، معمولاً API Token اشتباه است — سنایی توکن بد را هم 404 می‌دهد. "
                "توکن را دوباره از پنل کپی کنید."
            )
        return hint

    @staticmethod
    def _coerce_port(raw: Any) -> int | None:
        if raw is None or raw == "":
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _parse_inbounds_obj(self, data: Any) -> list[dict[str, Any]]:
        obj = data.get("obj") if isinstance(data, dict) else data
        if not isinstance(obj, list):
            return []
        out: list[dict[str, Any]] = []
        for item in obj:
            if not isinstance(item, dict) or "id" not in item:
                continue
            try:
                iid = int(item["id"])
            except (TypeError, ValueError):
                continue
            remark = item.get("remark") or item.get("tag") or ""
            if not isinstance(remark, str):
                remark = str(remark)
            protocol = item.get("protocol") or ""
            if not isinstance(protocol, str):
                protocol = str(protocol)
            out.append(
                {
                    "id": iid,
                    "remark": remark,
                    "protocol": protocol,
                    "port": self._coerce_port(item.get("port")),
                    "enable": bool(item.get("enable", True)),
                }
            )
        return out

    async def list_inbounds(self) -> list[dict[str, Any]]:
        if not self._token() and not (self.username and self.password):
            self._require_token()

        paths = (
            "panel/api/inbounds/list/slim",
            "panel/api/inbounds/list",
        )
        errors: list[str] = []

        # 1) httpx — slim first (short), then full (long read)
        for path in paths:
            try:
                resp = await self._request("GET", path)
                ok, data = ok_json(resp)
                if not ok:
                    errors.append(f"{path}: {err_msg(data, f'HTTP {resp.status_code}')}")
                    continue
                rows = self._parse_inbounds_obj(data)
                if rows:
                    return rows
                if path.endswith("/slim"):
                    errors.append(f"{path}: لیست خالی")
                    continue
                return rows
            except httpx.TimeoutException as exc:
                logger.warning("sanaei httpx timeout on %s: %s", path, exc)
                errors.append(f"{path}: ReadTimeout (httpx)")
                continue
            except httpx.HTTPError as exc:
                logger.warning("sanaei httpx error on %s: %s", path, exc)
                errors.append(f"{path}: {exc}")
                continue

        # 2) curl fallback (often OK through Cloudflare when httpx stalls)
        for path in paths:
            ok, data, status = await self._curl_json(
                path, max_time=90 if path.endswith("/slim") else 180
            )
            if not ok:
                errors.append(f"curl {path}: {err_msg(data, f'HTTP {status}')}")
                continue
            rows = self._parse_inbounds_obj(data)
            if rows or path.endswith("/list"):
                logger.info("sanaei inbounds via curl fallback (%s) count=%s", path, len(rows))
                return rows

        detail = " | ".join(errors[-4:]) if errors else "unknown"
        raise RuntimeError(
            "دریافت اینباند از سنایی ناموفق (پاسخ سنگین/کند از سرور شما). "
            f"جزئیات: {detail[:500]}"
        )

    async def _list_inbound_ids(self) -> list[int]:
        configured = self.meta.get("inbound_ids") or []
        if configured:
            return [int(x) for x in configured]
        return [int(i["id"]) for i in await self.list_inbounds()]

    async def test_connection(self) -> AdapterResult:
        try:
            if not self._token() and not (self.username and self.password):
                return AdapterResult(
                    success=False,
                    error="API Token یا یوزر/رمز سنایی را وارد کنید",
                )
            # slim first — full /list often times out on heavy Cloudflare panels
            last_resp = None
            last_data: Any = None
            for path in (
                "panel/api/inbounds/list/slim",
                "panel/api/inbounds/list",
                "panel/api/server/status",
            ):
                try:
                    resp = await self._request("GET", path)
                    last_resp = resp
                    ok, data = ok_json(resp)
                    last_data = data
                    if ok:
                        return AdapterResult(success=True, raw=data)
                except httpx.TimeoutException:
                    logger.warning("sanaei test_connection timeout on %s", path)
                    continue
                except httpx.HTTPError as exc:
                    logger.warning("sanaei test_connection error on %s: %s", path, exc)
                    continue
            if self._token():
                ok_c, data_c, _st = await self._curl_json(
                    "panel/api/inbounds/list/slim", max_time=90
                )
                if ok_c:
                    return AdapterResult(success=True, raw=data_c)
            if last_resp is not None:
                return AdapterResult(
                    success=False, error=self._explain_fail(last_resp, last_data)
                )
            return AdapterResult(success=False, error="timeout یا عدم دسترسی به API سنایی")
        except Exception as e:
            return AdapterResult(success=False, error=str(e))

    async def create_user(self, payload: UserPayload) -> AdapterResult:
        try:
            client_obj = self._client_payload(payload)
            inbound_ids = payload.config.get("inbound_ids") or await self._list_inbound_ids()
            if not inbound_ids:
                return AdapterResult(success=False, error="هیچ inbound فعالی در سنایی پیدا نشد")

            resp = await self._request(
                "POST",
                "panel/api/clients/add",
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

            settings_payload = json.dumps({"clients": [client_obj]})
            last_err = self._explain_fail(resp, data)
            for iid in inbound_ids:
                legacy = await self._request(
                    "POST",
                    "panel/api/inbounds/addClient",
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

            multi = await self._request(
                "POST",
                "panel/api/inbounds/addClientInbounds",
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
            resp = await self._request(
                "POST",
                f"panel/api/clients/update/{payload.username}",
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

            uuid = payload.uuid or client_obj["id"]
            inbound_ids = payload.config.get("inbound_ids") or await self._list_inbound_ids()
            settings_payload = json.dumps({"clients": [client_obj]})
            for iid in inbound_ids:
                r = await self._request(
                    "POST",
                    f"panel/api/inbounds/updateClient/{uuid}",
                    json={"id": iid, "settings": settings_payload},
                )
                rok, rdata = ok_json(r)
                if rok:
                    return AdapterResult(
                        success=True, remote_username=payload.username, uuid=uuid, raw=rdata
                    )
            return AdapterResult(success=False, error=self._explain_fail(resp, data))
        except Exception as e:
            return AdapterResult(success=False, error=str(e))

    async def delete_user(self, username: str, remote_id: str | None = None) -> AdapterResult:
        try:
            email = remote_id or username
            last_err = "delete failed"

            resp = await self._request("POST", f"panel/api/clients/del/{email}")
            ok, data = ok_json(resp)
            if ok:
                return AdapterResult(success=True, raw=data)
            last_err = err_msg(data, f"HTTP {resp.status_code}")
            low = last_err.lower()
            if any(x in low for x in ("not found", "404", "does not exist")):
                return AdapterResult(success=True, raw=data)

            inbound_ids = await self._list_inbound_ids()
            uuid = None
            try:
                t = await self._request("GET", f"panel/api/clients/traffic/{email}")
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
                        f"panel/api/inbounds/{iid}/delClient/{uuid}",
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

    @staticmethod
    def _sum_up_down(obj: Any) -> int | None:
        """Extract used bytes from traffic payload (dict or list of clients)."""
        if isinstance(obj, list):
            total = 0
            found = False
            for item in obj:
                if not isinstance(item, dict):
                    continue
                found = True
                total += int(item.get("up") or 0) + int(item.get("down") or 0)
            return total if found else None
        if isinstance(obj, dict):
            if "up" in obj or "down" in obj:
                return int(obj.get("up") or 0) + int(obj.get("down") or 0)
            # nested client / obj
            nested = obj.get("client") or obj.get("obj")
            if nested is not None and nested is not obj:
                return SanaeiAdapter._sum_up_down(nested)
        return None

    async def get_usage(self, username: str, remote_id: str | None = None) -> AdapterResult:
        try:
            email = (remote_id or username or "").strip()
            if not email:
                return AdapterResult(success=False, error="email/username خالی است")

            last_resp = None
            last_data: Any = None
            for path in (
                f"panel/api/clients/traffic/{email}",
                f"panel/api/inbounds/getClientTraffics/{email}",
                f"panel/api/inbounds/getClientTrafficsByEmail/{email}",
            ):
                try:
                    resp = await self._request("GET", path)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("sanaei get_usage %s: %s", path, exc)
                    continue
                last_resp = resp
                ok, data = ok_json(resp)
                last_data = data
                if not ok:
                    continue
                obj: Any = data
                if isinstance(data, dict):
                    obj = data.get("obj") if data.get("obj") is not None else data
                used = self._sum_up_down(obj)
                if used is not None:
                    return AdapterResult(
                        success=True,
                        remote_username=email,
                        used_traffic=used,
                        raw=data,
                    )

            if last_resp is not None:
                return AdapterResult(
                    success=False, error=self._explain_fail(last_resp, last_data)
                )
            return AdapterResult(success=False, error="دریافت ترافیک سنایی ناموفق")
        except Exception as e:
            return AdapterResult(success=False, error=str(e))

    async def _fetch_links(self, email: str, sub_id: str | None = None) -> list[str]:
        links: list[str] = []
        try:
            resp = await self._request("GET", f"panel/api/clients/links/{email}")
            ok, data = ok_json(resp)
            if ok and isinstance(data, dict):
                obj = data.get("obj") or data
                links.extend(self._extract_links(obj))
        except Exception:
            pass
        if sub_id:
            try:
                resp = await self._request("GET", f"panel/api/clients/subLinks/{sub_id}")
                ok, data = ok_json(resp)
                if ok and isinstance(data, dict):
                    links.extend(self._extract_links(data.get("obj") or data))
            except Exception:
                pass
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
