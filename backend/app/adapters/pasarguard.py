"""PasarGuard adapter — username/password token auth + group_ids user model."""

from __future__ import annotations

from typing import Any

from app.adapters.base import AdapterResult, UserPayload
from app.adapters.http_utils import err_msg, expand_subscription_links, ok_json
from app.adapters.marzban import MarzbanAdapter


class PasarGuardAdapter(MarzbanAdapter):
    """
    PasarGuard (پاسارگاد):
    - Auth: POST /api/admin/token with username/password (OAuth2 form)
    - Users require group_ids (inbounds come from groups)
    - Optional X-Api-Key if api_token stored as pg_key_...
    """

    async def _ensure_token(self) -> str:
        if self.api_token and str(self.api_token).startswith("pg_"):
            self._token = self.api_token
            return self._token
        return await super()._ensure_token()

    async def _headers(self) -> dict[str, str]:
        token = await self._ensure_token()
        if self.api_token and str(self.api_token).startswith("pg_"):
            return {"X-Api-Key": token, "Accept": "application/json"}
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _parse_groups_payload(self, data: Any) -> list[dict[str, Any]] | None:
        """Normalize PasarGuard group list payloads. None = unusable response."""
        groups: list = []
        if isinstance(data, list):
            groups = data
        elif isinstance(data, dict):
            raw = data.get("groups")
            if raw is None:
                raw = data.get("items") or data.get("obj")
            if isinstance(raw, list):
                groups = raw
            else:
                return None
        else:
            return None

        out: list[dict[str, Any]] = []
        for g in groups:
            if not isinstance(g, dict) or g.get("id") is None:
                continue
            out.append(
                {
                    "id": int(g["id"]),
                    "name": str(g.get("name") or g.get("title") or f"Group #{g['id']}"),
                    "is_disabled": bool(g.get("is_disabled") or g.get("disabled") or False),
                }
            )
        return out

    async def _groups_from_admin_profile(self, headers: dict[str, str]) -> list[dict[str, Any]] | None:
        """
        Operator roles often only have groups.read_simple / allowed_group_ids.
        Fall back to current admin profile when list endpoints are denied.
        """
        client = await self._get_client()
        resp = await client.get("/api/admin", headers=headers)
        ok, data = ok_json(resp)
        if not ok or not isinstance(data, dict):
            return None

        # role.access.allowed_group_ids — common for operators/resellers
        role = data.get("role") if isinstance(data.get("role"), dict) else {}
        access = role.get("access") if isinstance(role.get("access"), dict) else {}
        allowed = access.get("allowed_group_ids")
        if isinstance(allowed, list) and allowed:
            return [
                {
                    "id": int(gid),
                    "name": f"Group #{int(gid)}",
                    "is_disabled": False,
                }
                for gid in allowed
                if gid is not None
            ]
        return None

    async def list_groups(self) -> list[dict[str, Any]]:
        """
        Fetch remote groups from PasarGuard (works for owner + operator).

        Operator role typically has:
          groups.read = false
          groups.read_simple = true
        so prefer GET /api/groups/simple.

        NOTE: GET /api/group is invalid (POST-only create) → 405.
        """
        client = await self._get_client()
        headers = await self._headers()
        # Operators: read_simple only. Owners: both work.
        attempts: list[tuple[str, dict[str, Any] | None]] = [
            ("/api/groups/simple", {"all": "true"}),
            ("/api/groups/simple", {"limit": "1000", "offset": "0"}),
            ("/api/groups/simple", None),
            ("/api/groups", {"limit": "1000", "offset": "0"}),
            ("/api/groups", None),
        ]
        errors: list[str] = []
        for path, params in attempts:
            resp = await client.get(path, headers=headers, params=params)
            ok, data = ok_json(resp)
            if not ok:
                errors.append(f"{path}: {err_msg(data, f'HTTP {resp.status_code}')}")
                continue
            parsed = self._parse_groups_payload(data)
            if parsed is None:
                errors.append(f"{path}: unexpected response shape")
                continue
            return parsed

        # Last resort for restricted operators
        fallback = await self._groups_from_admin_profile(headers)
        if fallback is not None:
            return fallback

        hint = (
            "اگر آدرس پنل را با /dashboard وارد کرده‌اید، فقط ریشه دامنه را بگذارید "
            "(مثلاً https://panel.example.com:8443)."
        )
        raise RuntimeError((" | ".join(errors) if errors else "Cannot list groups") + f" — {hint}")

    async def _resolve_group_ids(self, payload: UserPayload) -> list[int]:
        configured = (
            payload.config.get("remote_group_ids")
            or payload.config.get("group_ids")
            or self.meta.get("default_group_ids")
            or []
        )
        if configured:
            return [int(x) for x in configured]

        groups = await self.list_groups()
        ids = [int(g["id"]) for g in groups if not g.get("is_disabled")]
        if ids:
            return ids
        raise RuntimeError(
            "هیچ گروهی در پنل پاسارگاد پیدا نشد؛ ابتدا در پنل گروه بسازید یا default_group_ids در meta تنظیم کنید"
        )

    def _build_pg_body(self, payload: UserPayload, group_ids: list[int]) -> dict[str, Any]:
        # PasarGuard (Marzban-family): pass through, map unknown → disabled
        _ok = ("active", "on_hold", "disabled", "limited", "expired")
        status = payload.status if payload.status in _ok else "disabled"
        body: dict[str, Any] = {
            "username": payload.username,
            "status": status,
            "group_ids": group_ids,
            "data_limit": int(payload.data_limit or 0),
            "data_limit_reset_strategy": payload.config.get("data_limit_reset_strategy", "no_reset"),
            "note": payload.note or "",
            "proxy_settings": payload.config.get("proxy_settings") or {},
        }
        # PasarGuard accepts expire unix timestamp (0/null = unlimited)
        body["expire"] = int(payload.expire or 0) or None
        limit_ip = payload.config.get("limit_ip")
        if limit_ip is not None:
            try:
                n = int(limit_ip)
                if n >= 0:
                    # common field names across PG builds
                    body["max_ips"] = n
                    proxy = dict(body.get("proxy_settings") or {})
                    proxy["limit_ip"] = n
                    body["proxy_settings"] = proxy
            except (TypeError, ValueError):
                pass
        return body

    def _from_pg_response(self, data: dict, fallback: str) -> AdapterResult:
        links = list(data.get("links") or [])
        sub = data.get("subscription_url")
        if sub and str(sub) not in links:
            links.append(str(sub))
        return AdapterResult(
            success=True,
            remote_username=data.get("username", fallback),
            remote_id=str(data.get("id") or data.get("username") or fallback),
            uuid=None,
            links=links,
            used_traffic=self._parse_used_traffic(data),
            raw=data,
        )

    async def _finalize_pg(self, result: AdapterResult) -> AdapterResult:
        client = await self._get_client()
        result.links = await expand_subscription_links(client, list(result.links or []))
        return result

    async def test_connection(self) -> AdapterResult:
        try:
            client = await self._get_client()
            headers = await self._headers()
            resp = await client.get("/api/admin", headers=headers)
            ok, data = ok_json(resp)
            if ok:
                return AdapterResult(success=True, raw=data)
            # some builds expose /api/system
            resp2 = await client.get("/api/system", headers=headers)
            ok2, data2 = ok_json(resp2)
            if ok2:
                return AdapterResult(success=True, raw=data2)
            return AdapterResult(success=False, error=err_msg(data, f"HTTP {resp.status_code}"))
        except Exception as e:
            return AdapterResult(success=False, error=str(e))

    async def create_user(self, payload: UserPayload) -> AdapterResult:
        try:
            group_ids = await self._resolve_group_ids(payload)
            client = await self._get_client()
            headers = await self._headers()
            body = self._build_pg_body(payload, group_ids)
            resp = await client.post("/api/user", headers=headers, json=body)
            ok, data = ok_json(resp)
            if ok and isinstance(data, dict):
                return await self._finalize_pg(self._from_pg_response(data, payload.username))
            return AdapterResult(success=False, error=err_msg(data, f"HTTP {resp.status_code}"))
        except Exception as e:
            return AdapterResult(success=False, error=str(e))

    async def modify_user(self, payload: UserPayload) -> AdapterResult:
        try:
            group_ids = await self._resolve_group_ids(payload)
            client = await self._get_client()
            headers = await self._headers()
            body = self._build_pg_body(payload, group_ids)
            body.pop("username", None)
            resp = await client.put(f"/api/user/{payload.username}", headers=headers, json=body)
            ok, data = ok_json(resp)
            if ok and isinstance(data, dict):
                return await self._finalize_pg(self._from_pg_response(data, payload.username))
            return AdapterResult(success=False, error=err_msg(data, f"HTTP {resp.status_code}"))
        except Exception as e:
            return AdapterResult(success=False, error=str(e))
