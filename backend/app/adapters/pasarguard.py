"""PasarGuard adapter — username/password token auth + group_ids user model."""

from __future__ import annotations

from typing import Any

from app.adapters.base import AdapterResult, UserPayload
from app.adapters.http_utils import err_msg, ok_json
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
        cached = getattr(self, "_groups_memo", None)
        if cached is not None:
            return cached
        client, headers = await self._authed()
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
            self._groups_memo = parsed
            return parsed

        # Last resort for restricted operators
        fallback = await self._groups_from_admin_profile(headers)
        if fallback is not None:
            self._groups_memo = fallback
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

    @staticmethod
    def _want_disabled(payload: UserPayload) -> bool:
        """Astra limited/expired/disabled → PasarGuard must cut traffic (disabled=true)."""
        return (payload.status or "").lower() in ("limited", "expired", "disabled")

    async def _resolve_numeric_user_id(
        self,
        username: str,
        *,
        hint: str | int | None = None,
    ) -> int | None:
        """PasarGuard disable API needs numeric id: /api/user/by-id/{id}/disabled."""
        if hint is not None:
            s = str(hint).strip()
            if s.isdigit():
                return int(s)
        uname = (username or "").strip()
        if not uname:
            return None
        try:
            import httpx

            client, headers = await self._authed()
            resp = await client.get(
                f"/api/user/{uname}",
                headers=headers,
                timeout=httpx.Timeout(12.0, connect=6.0),
            )
            ok, data = ok_json(resp)
            if ok and isinstance(data, dict) and data.get("id") is not None:
                return int(data["id"])
        except Exception:
            return None
        return None

    async def _set_disabled_by_id(self, user_id: int, disabled: bool) -> AdapterResult:
        """PUT /api/user/by-id/{id}/disabled  body: {\"disabled\": true|false}

        This is what PasarGuard UI uses to turn a user off in edit — status-only
        modify does not reliably stop traffic when Astra quota (with coeff) ends.
        """
        try:
            import httpx

            uid = int(user_id)
            client, headers = await self._authed()
            resp = await client.put(
                f"/api/user/by-id/{uid}/disabled",
                headers=headers,
                json={"disabled": bool(disabled)},
                timeout=httpx.Timeout(15.0, connect=6.0),
            )
            ok, data = ok_json(resp)
            if ok or resp.status_code in (200, 204):
                return AdapterResult(
                    success=True,
                    remote_id=str(uid),
                    raw=data if isinstance(data, dict) else {"disabled": bool(disabled)},
                )
            return AdapterResult(
                success=False,
                remote_id=str(uid),
                error=err_msg(data, f"HTTP {resp.status_code}"),
            )
        except Exception as e:
            return AdapterResult(
                success=False,
                remote_id=str(user_id),
                error=str(e).strip() or e.__class__.__name__,
            )

    def _build_pg_body(self, payload: UserPayload, group_ids: list[int]) -> dict[str, Any]:
        # PasarGuard (Marzban-family): pass through, map unknown → disabled
        _ok = ("active", "on_hold", "disabled", "limited", "expired")
        status = payload.status if payload.status in _ok else "disabled"
        # Freeze path: UI "خاموش" uses disabled flag; mirror status for edit screen
        if status in ("limited", "expired"):
            status = "disabled"
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

    async def test_connection(self) -> AdapterResult:
        try:
            client, headers = await self._authed()
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
            return AdapterResult(success=False, error=str(e).strip() or e.__class__.__name__)

    async def create_user(self, payload: UserPayload) -> AdapterResult:
        """POST /api/user — PasarGuard docs; retry on network blips; idempotent if exists."""
        import asyncio

        import httpx

        try:
            group_ids = await self._resolve_group_ids(payload)
        except Exception as e:
            return AdapterResult(
                success=False,
                error=f"resolve group_ids: {str(e).strip() or e.__class__.__name__}",
            )
        body = self._build_pg_body(payload, group_ids)
        last_err = "create failed"
        # Keep under orchestrator create_timeout (~20s): 2 tries, short connect
        req_timeout = httpx.Timeout(16.0, connect=5.0)
        for attempt in range(2):
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
                    # Fast create — expand /sub later on subscription hydrate
                    return self._from_pg_response(data, payload.username)
                err = err_msg(data, f"HTTP {resp.status_code}")
                low = err.lower()
                # "does not exist" / "not found" must NOT match the "already exists" branch
                not_found = any(
                    x in low
                    for x in (
                        "not found",
                        "does not exist",
                        "doesn't exist",
                        "یافت نشد",
                        "پیدا نشد",
                        "وجود ندارد",
                    )
                )
                already = resp.status_code == 409 or (
                    (not not_found)
                    and any(
                        x in low
                        for x in ("already", "duplicate", "تکرار", "موجود", "already exist")
                    )
                ) or ((not not_found) and "exist" in low and "not" not in low)
                if already:
                    # 409 on PasarGuard can mean "username reserved" while GET 404
                    # (other admin / soft-delete). Never fake success — verify or modify.
                    existing = await self.get_usage(payload.username, expand_links=False)
                    if existing.success:
                        return existing
                    try:
                        modified = await self.modify_user(payload)
                        if modified.success:
                            return modified
                        mod_err = modified.error or "modify failed"
                    except Exception as me:  # noqa: BLE001
                        mod_err = str(me).strip() or me.__class__.__name__
                    return AdapterResult(
                        success=False,
                        error=(
                            f"پنل می‌گوید کاربر از قبل هست (HTTP {resp.status_code}: {err}) "
                            f"ولی خواندن ناموفق بود و به‌روزرسانی هم شکست خورد ({mod_err}). "
                            f"احتمالاً یوزرنیم برای ادمین دیگر است یا حذف‌نرم شده. "
                            f"group_ids={group_ids}"
                        ),
                    )
                last_err = f"{err} (group_ids={group_ids})"
                if resp.status_code in (401, 403) and attempt < 1:
                    self._token = None
                    from app.adapters.marzban import _TOKEN_CACHE

                    _TOKEN_CACHE.pop(self._cache_key(), None)
                    await self.close()
                    continue
                if attempt < 1 and resp.status_code >= 500:
                    await self.close()
                    await asyncio.sleep(0.25)
                    continue
                return AdapterResult(success=False, error=last_err)
            except Exception as e:
                last_err = str(e).strip() or e.__class__.__name__
                try:
                    existing = await asyncio.wait_for(
                        self.get_usage(payload.username, expand_links=False), timeout=4
                    )
                    if existing.success:
                        return existing
                except Exception:
                    pass
                if attempt < 1:
                    self._token = None
                    from app.adapters.marzban import _TOKEN_CACHE

                    _TOKEN_CACHE.pop(self._cache_key(), None)
                    await self.close()
                    await asyncio.sleep(0.2)
                    continue
                return AdapterResult(
                    success=False,
                    error=f"{last_err} (group_ids={group_ids})",
                )
        return AdapterResult(success=False, error=f"{last_err} (group_ids={group_ids})")

    async def modify_user(self, payload: UserPayload) -> AdapterResult:
        """PUT /api/user/{username} + PUT /api/user/by-id/{id}/disabled.

        Quota freeze on Astra must call the same disable endpoint as PasarGuard's
        edit UI ({disabled: true}); status/data_limit alone leaves leftover traffic
        usable when coefficients exhaust Astra before the raw remote cap.
        """
        want_disabled = self._want_disabled(payload)
        hint = (payload.config or {}).get("_remote_id") or (payload.config or {}).get(
            "remote_id"
        )
        mod = AdapterResult(success=False, error="modify not attempted")
        group_ids: list[int] = []
        try:
            group_ids = await self._resolve_group_ids(payload)
            body = self._build_pg_body(payload, group_ids)
            body.pop("username", None)
            client, headers = await self._authed()
            resp = await client.put(
                f"/api/user/{payload.username}",
                headers=headers,
                json=body,
                timeout=__import__("httpx").Timeout(22.0, connect=8.0),
            )
            ok, data = ok_json(resp)
            if ok and isinstance(data, dict):
                mod = self._from_pg_response(data, payload.username)
            else:
                err = err_msg(data, f"HTTP {resp.status_code}")
                mod = AdapterResult(
                    success=False,
                    error=f"{err} (group_ids={group_ids})",
                )
        except Exception as e:
            mod = AdapterResult(
                success=False, error=str(e).strip() or e.__class__.__name__
            )

        uid: int | None = None
        if mod.success and mod.remote_id and str(mod.remote_id).strip().isdigit():
            uid = int(str(mod.remote_id).strip())
        if uid is None:
            uid = await self._resolve_numeric_user_id(payload.username, hint=hint)

        if uid is None:
            if want_disabled:
                return AdapterResult(
                    success=False,
                    error=(
                        (mod.error + " | " if mod.error else "")
                        + "PasarGuard user id پیدا نشد؛ خاموش‌سازی by-id ممکن نیست"
                    ),
                    remote_username=payload.username,
                )
            return mod

        dis = await self._set_disabled_by_id(uid, want_disabled)
        if want_disabled:
            if dis.success:
                if mod.success:
                    mod.remote_id = str(uid)
                    return mod
                return AdapterResult(
                    success=True,
                    remote_username=payload.username,
                    remote_id=str(uid),
                    raw=dis.raw,
                )
            return AdapterResult(
                success=False,
                remote_username=payload.username,
                remote_id=str(uid),
                error=(
                    f"خاموش‌سازی پاسارگاد ناموفق: {dis.error or 'unknown'}"
                    + (f" | modify: {mod.error}" if mod.error else "")
                ),
            )

        # Re-enable path: disabled=false must succeed
        if not dis.success:
            return AdapterResult(
                success=False,
                remote_username=payload.username,
                remote_id=str(uid),
                error=f"روشن‌سازی پاسارگاد (disabled=false) ناموفق: {dis.error or 'unknown'}",
            )
        if mod.success:
            mod.remote_id = str(uid)
            return mod
        return AdapterResult(
            success=True,
            remote_username=payload.username,
            remote_id=str(uid),
            raw=dis.raw,
        )
