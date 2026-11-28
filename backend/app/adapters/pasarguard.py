"""PasarGuard adapter — username/password token auth + group_ids user model."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app.adapters.base import AdapterResult, UserPayload, apply_remote_state
from app.adapters.http_utils import err_msg, ok_json
from app.adapters.marzban import MarzbanAdapter

logger = logging.getLogger("astra.adapter.pasarguard")

# Process-wide short cache so Groups UI reopen / multi-panel apply stays snappy.
# value: (groups, fresh_until, hard_expire) — serve stale until hard_expire while
# a refresh is in flight so the Groups modal never sits on «در حال دریافت…».
_PG_GROUPS_CACHE: dict[str, tuple[list[dict[str, Any]], float, float]] = {}
_PG_GROUPS_TTL = 120.0
_PG_GROUPS_STALE_TTL = 900.0  # serve stale up to 15 min if remote is slow
_PG_GROUPS_REFRESHING: set[str] = set()


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
        resp = await client.get(
            "/api/admin",
            headers=headers,
            timeout=httpx.Timeout(5.0, connect=2.5),
        )
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

    def _groups_cache_key(self) -> str:
        return (
            f"{(self.base_url or '').rstrip('/')}"
            f"|{(self.username or '').strip()}"
            f"|{(self.api_token or '')[:12]}"
        )

    def _store_groups_cache(
        self, cache_key: str, groups: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        now = time.monotonic()
        _PG_GROUPS_CACHE[cache_key] = (
            groups,
            now + _PG_GROUPS_TTL,
            now + _PG_GROUPS_STALE_TTL,
        )
        self._groups_memo = groups
        return groups

    async def list_groups(self) -> list[dict[str, Any]]:
        """
        Fetch remote groups from PasarGuard (works for owner + operator).

        Operator role typically has:
          groups.read = false
          groups.read_simple = true
        so prefer GET /api/groups/simple.

        NOTE: GET /api/group is invalid (POST-only create) → 405.

        Fast path: return fresh/stale cache immediately. Refresh races only the
        two most useful endpoints with a tight budget so Groups UI is snappy.
        """
        cached = getattr(self, "_groups_memo", None)
        if cached is not None:
            return cached
        cache_key = self._groups_cache_key()
        now = time.monotonic()
        hit = _PG_GROUPS_CACHE.get(cache_key)
        if hit is not None:
            groups, fresh_until, hard_expire = hit
            if now < fresh_until:
                self._groups_memo = groups
                return groups
            # Stale but usable — return now, refresh in background once.
            if now < hard_expire and cache_key not in _PG_GROUPS_REFRESHING:
                _PG_GROUPS_REFRESHING.add(cache_key)

                async def _bg_refresh() -> None:
                    try:
                        await self._fetch_groups_remote(cache_key, allow_stale=False)
                    except Exception:  # noqa: BLE001
                        pass
                    finally:
                        _PG_GROUPS_REFRESHING.discard(cache_key)

                try:
                    asyncio.get_running_loop().create_task(
                        _bg_refresh(), name=f"pg-groups-refresh-{cache_key[:24]}"
                    )
                except Exception:  # noqa: BLE001
                    _PG_GROUPS_REFRESHING.discard(cache_key)
                self._groups_memo = groups
                return groups

        return await self._fetch_groups_remote(cache_key, allow_stale=True)

    async def _fetch_groups_remote(
        self, cache_key: str, *, allow_stale: bool
    ) -> list[dict[str, Any]]:
        client, headers = await self._authed()
        # Two best shots first — owners + operators. Extra shapes only as fallback.
        primary: list[tuple[str, dict[str, Any] | None]] = [
            ("/api/groups/simple", {"all": "true"}),
            ("/api/groups", {"limit": "1000", "offset": "0"}),
        ]
        secondary: list[tuple[str, dict[str, Any] | None]] = [
            ("/api/groups/simple", {"limit": "1000", "offset": "0"}),
            ("/api/groups/simple", None),
            ("/api/groups", None),
        ]
        req_timeout = httpx.Timeout(3.5, connect=1.8)
        errors: list[str] = []

        async def _try_one(
            path: str, params: dict[str, Any] | None
        ) -> list[dict[str, Any]]:
            resp = await client.get(
                path, headers=headers, params=params, timeout=req_timeout
            )
            ok, data = ok_json(resp)
            if not ok:
                raise RuntimeError(
                    f"{path}: {err_msg(data, f'HTTP {resp.status_code}')}"
                )
            parsed = self._parse_groups_payload(data)
            if parsed is None:
                raise RuntimeError(f"{path}: unexpected response shape")
            return parsed

        async def _race(
            attempts: list[tuple[str, dict[str, Any] | None]], budget: float
        ) -> list[dict[str, Any]] | None:
            tasks = [
                asyncio.create_task(_try_one(path, params), name=f"pg-groups-{path}")
                for path, params in attempts
            ]
            winner: list[dict[str, Any]] | None = None
            try:
                for fut in asyncio.as_completed(tasks, timeout=budget):
                    try:
                        winner = await fut
                        break
                    except Exception as exc:  # noqa: BLE001
                        errors.append(str(exc).strip() or exc.__class__.__name__)
            except asyncio.TimeoutError:
                errors.append("timeout waiting for group list")
            finally:
                for t in tasks:
                    if not t.done():
                        t.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            return winner

        winner = await _race(primary, 4.0)
        if winner is None:
            winner = await _race(secondary, 3.0)

        if winner is not None:
            return self._store_groups_cache(cache_key, winner)

        # Last resort for restricted operators
        try:
            fallback = await asyncio.wait_for(
                self._groups_from_admin_profile(headers), timeout=3.0
            )
        except Exception:  # noqa: BLE001
            fallback = None
        if fallback is not None:
            return self._store_groups_cache(cache_key, fallback)

        if allow_stale:
            stale = _PG_GROUPS_CACHE.get(cache_key)
            if stale is not None and time.monotonic() < stale[2]:
                self._groups_memo = stale[0]
                return stale[0]

        hint = (
            "اگر آدرس پنل را با /dashboard وارد کرده‌اید، فقط ریشه دامنه را بگذارید "
            "(مثلاً https://panel.example.com:8443)."
        )
        raise RuntimeError(
            (" | ".join(errors) if errors else "Cannot list groups") + f" — {hint}"
        )

    async def _resolve_group_ids(self, payload: UserPayload) -> list[int]:
        """Return remote group ids that actually exist on this panel.

        Stale ``remote_group_ids`` in Astra group×panel config (deleted on the
        panel) used to make every create fail with «Group not found» and then
        surface as «کاربر روی این پنل وجود ندارد». Drop missing ids and keep
        the rest so provision stays fast and accurate.
        """
        configured_raw = (
            payload.config.get("remote_group_ids")
            or payload.config.get("group_ids")
            or self.meta.get("default_group_ids")
            or []
        )
        configured: list[int] = []
        for x in configured_raw or []:
            try:
                i = int(x)
            except (TypeError, ValueError):
                continue
            if i > 0 and i not in configured:
                configured.append(i)

        groups = await self.list_groups()
        live_enabled = {
            int(g["id"])
            for g in groups
            if g.get("id") is not None and not g.get("is_disabled")
        }
        live_all = {
            int(g["id"]) for g in groups if g.get("id") is not None
        }

        if configured:
            # Prefer enabled; if an id exists but is disabled, still skip it.
            filtered = [i for i in configured if i in live_enabled]
            if filtered:
                dropped = [i for i in configured if i not in live_enabled]
                if dropped:
                    logger.warning(
                        "pasarguard drop missing/disabled group_ids=%s keep=%s url=%s",
                        dropped,
                        filtered,
                        (self.base_url or "")[:80],
                    )
                return filtered
            # Configured ids are all gone — clear error, do not invent all groups.
            raise RuntimeError(
                "Group not found — هیچ‌کدام از group_ids تنظیم‌شده روی این پنل "
                f"نیستند: {configured} (موجود: {sorted(live_all)})"
            )

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
        from app.adapters.http_utils import is_emfile_err

        if hint is not None:
            s = str(hint).strip()
            if s.isdigit():
                return int(s)
        uname = (username or "").strip()
        if not uname:
            return None
        for attempt in range(3):
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
                return None
            except Exception as e:
                if attempt < 2 and is_emfile_err(e):
                    reset = getattr(self, "_reset_client", None)
                    if callable(reset):
                        try:
                            await reset()
                        except Exception:
                            pass
                    await asyncio.sleep(0.35 + attempt * 0.25)
                    continue
                return None
        return None

    async def _set_disabled_by_id(self, user_id: int, disabled: bool) -> AdapterResult:
        """PUT /api/user/by-id/{id}/disabled  body: {\"disabled\": true|false}

        This is what PasarGuard UI uses to turn a user off in edit — status-only
        modify does not reliably stop traffic when Astra quota (with coeff) ends.
        """
        from app.adapters.http_utils import is_emfile_err

        uid = int(user_id)
        last_err = "disable failed"
        for attempt in range(3):
            try:
                import httpx

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
                        raw=data
                        if isinstance(data, dict)
                        else {"disabled": bool(disabled)},
                    )
                return AdapterResult(
                    success=False,
                    remote_id=str(uid),
                    error=err_msg(data, f"HTTP {resp.status_code}"),
                )
            except Exception as e:
                last_err = str(e).strip() or e.__class__.__name__
                if attempt < 2 and is_emfile_err(e):
                    reset = getattr(self, "_reset_client", None)
                    if callable(reset):
                        try:
                            await reset()
                        except Exception:
                            pass
                    await asyncio.sleep(0.35 + attempt * 0.25)
                    continue
                return AdapterResult(
                    success=False,
                    remote_id=str(user_id),
                    error=last_err,
                )
        return AdapterResult(
            success=False, remote_id=str(user_id), error=last_err
        )

    async def reset_usage(
        self, username: str, remote_id: str | None = None
    ) -> AdapterResult:
        """Reset traffic AND clear PasarGuard ``disabled`` flag.

        Astra quota freeze uses PUT /api/user/by-id/{id}/disabled. Marzban-style
        POST /reset alone leaves ``disabled=true``, so configs stay cut on the
        main panel after admin «بازنشانی مصرف».
        """
        uname = (username or "").strip()
        if not uname:
            return AdapterResult(success=False, error="username خالی است")
        # 1) zero remote counters (inherited Marzban path)
        res = await MarzbanAdapter.reset_usage(self, uname, remote_id)
        # 2) always re-enable — even if reset body looked odd
        uid: int | None = None
        if remote_id and str(remote_id).strip().isdigit():
            uid = int(str(remote_id).strip())
        if uid is None and res.remote_id and str(res.remote_id).strip().isdigit():
            uid = int(str(res.remote_id).strip())
        if uid is None:
            try:
                uid = await self._resolve_numeric_user_id(uname, hint=remote_id)
            except Exception:  # noqa: BLE001
                uid = None
        if uid is not None:
            dis = await self._set_disabled_by_id(uid, False)
            if not dis.success:
                # Reset may have worked but user stays offline — surface enable fail.
                return AdapterResult(
                    success=False,
                    remote_username=uname,
                    remote_id=str(uid),
                    used_traffic=res.used_traffic,
                    error=(
                        f"بازنشانی شد ولی روشن‌سازی پاسارگاد ناموفق: "
                        f"{dis.error or 'unknown'}"
                    ),
                    raw=dis.raw,
                )
        # 3) fresh read so callers see post-reset used + active
        try:
            linked = await self.get_usage(uname, remote_id or (str(uid) if uid else None))
            if linked.success:
                if uid is not None:
                    linked.remote_id = str(uid)
                return linked
        except Exception:  # noqa: BLE001
            pass
        if res.success and uid is not None:
            res.remote_id = str(uid)
        return res

    def _build_pg_body(self, payload: UserPayload, group_ids: list[int]) -> dict[str, Any]:
        # Cloud PasarGuard create/modify schema only accepts status ∈ {active, on_hold}.
        # Freeze (limited/expired/disabled) is done via PUT .../disabled, not status=.
        raw = (payload.status or "").lower().strip()
        if raw == "on_hold":
            status = "on_hold"
        else:
            status = "active"
        body: dict[str, Any] = {
            "username": payload.username,
            "status": status,
            "group_ids": group_ids,
            "data_limit": int(payload.data_limit or 0),
            "data_limit_reset_strategy": payload.config.get(
                "data_limit_reset_strategy", "no_reset"
            ),
            "note": payload.note or "",
            "proxy_settings": payload.config.get("proxy_settings") or {},
        }
        # PasarGuard accepts expire unix timestamp (0/null = unlimited)
        body["expire"] = int(payload.expire or 0) or None
        if status == "on_hold":
            # Docs: status=on_hold + on_hold_expire_duration; expire must be empty
            body["expire"] = None
            try:
                hold_dur = int((payload.config or {}).get("on_hold_expire_duration") or 0)
            except (TypeError, ValueError):
                hold_dur = 0
            if hold_dur > 0:
                body["on_hold_expire_duration"] = hold_dur
        limit_ip = payload.config.get("limit_ip")
        if limit_ip is not None:
            try:
                n = int(limit_ip)
                if n >= 0:
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
        return apply_remote_state(
            AdapterResult(
                success=True,
                remote_username=data.get("username", fallback),
                remote_id=str(data.get("id") or data.get("username") or fallback),
                uuid=None,
                links=links,
                used_traffic=self._parse_used_traffic(data),
                raw=data,
            ),
            data,
        )

    async def test_connection(self) -> AdapterResult:
        """Prove admin token — one sharp probe (test-all owns retries)."""
        import httpx

        from app.adapters.marzban import _TOKEN_CACHE

        last_err = "اتصال ناموفق"
        quick = bool((self.meta or {}).get("_probe_quick"))
        # Keep connect ≥1.2s so flaky CF/tunnel panels still handshake.
        probe_t = httpx.Timeout(2.4 if quick else 3.5, connect=1.25 if quick else 1.6)
        try:
            client, headers = await self._authed()
            resp = await client.get(
                "/api/admin",
                headers=headers,
                timeout=probe_t,
            )
            ok, data = ok_json(resp)
            if ok:
                return AdapterResult(
                    success=True, raw=data if isinstance(data, dict) else {}
                )
            if resp.status_code in (401, 403):
                last_err = err_msg(data, f"HTTP {resp.status_code}")
                self._token = None
                _TOKEN_CACHE.pop(self._cache_key(), None)
                await self.close()
                try:
                    client, headers = await self._authed()
                    resp = await client.get(
                        "/api/admin",
                        headers=headers,
                        timeout=probe_t,
                    )
                    ok, data = ok_json(resp)
                    if ok:
                        return AdapterResult(
                            success=True,
                            raw=data if isinstance(data, dict) else {},
                        )
                    last_err = err_msg(data, f"HTTP {resp.status_code}")
                except Exception as e:  # noqa: BLE001
                    last_err = str(e).strip() or last_err
                return AdapterResult(success=False, error=last_err)
            # /api/system only when /api/admin is missing — skip on quick misses
            # that already got a real HTTP answer (avoids +RTT on test-all).
            if quick and resp.status_code not in (404, 405, 501):
                last_err = err_msg(data, f"HTTP {resp.status_code}")
                return AdapterResult(success=False, error=last_err)
            resp2 = await client.get(
                "/api/system",
                headers=headers,
                timeout=probe_t,
            )
            ok2, data2 = ok_json(resp2)
            if ok2:
                return AdapterResult(
                    success=True,
                    raw=data2 if isinstance(data2, dict) else {},
                )
            last_err = err_msg(data2, err_msg(data, f"HTTP {resp.status_code}"))
            return AdapterResult(success=False, error=last_err)
        except Exception as e:  # noqa: BLE001
            last_err = str(e).strip() or e.__class__.__name__
            return AdapterResult(success=False, error=last_err)

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
        # Keep under orchestrator create_timeout (~28s): 2 tries, short connect
        req_timeout = httpx.Timeout(22.0, connect=6.0)
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
                    created = self._from_pg_response(data, payload.username)
                    # Create schema forbids status=disabled; freeze via by-id API
                    if self._want_disabled(payload):
                        uid = None
                        if created.remote_id and str(created.remote_id).strip().isdigit():
                            uid = int(str(created.remote_id).strip())
                        if uid is None:
                            uid = await self._resolve_numeric_user_id(
                                created.remote_username or payload.username
                            )
                        if uid is not None:
                            dis = await self._set_disabled_by_id(uid, True)
                            if not dis.success:
                                created.error = (
                                    f"ساخته شد ولی خاموش‌سازی ناموفق: {dis.error or '?'}"
                                )
                            created.remote_id = str(uid)
                    return created
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
                # Group user-cap full: panel is fine. If the username already
                # exists (partial create / other path), adopt instead of failing.
                cap = "limit reached" in low or "user limit" in low
                if cap:
                    existing = await self.get_usage(
                        payload.username, expand_links=False
                    )
                    if existing.success:
                        return existing
                    return AdapterResult(
                        success=False,
                        error=(
                            f"User limit reached — ظرفیت گروه پر است "
                            f"(group_ids={group_ids}): {err}"
                        ),
                    )
                # Stale group ids slipped through cache — refresh + retry once.
                if (
                    attempt < 1
                    and ("group not found" in low or "group_ids" in low)
                    and ("not found" in low or "یافت" in err or "پیدا" in err)
                ):
                    self._groups_memo = None
                    from app.adapters import pasarguard as _pg_mod

                    _pg_mod._PG_GROUPS_CACHE.pop(self._groups_cache_key(), None)
                    try:
                        group_ids = await self._resolve_group_ids(payload)
                        body = self._build_pg_body(payload, group_ids)
                    except Exception as re:  # noqa: BLE001
                        return AdapterResult(
                            success=False,
                            error=(
                                f"{err} — پس از همگام‌سازی گروه‌ها: "
                                f"{str(re).strip() or re.__class__.__name__}"
                            ),
                        )
                    await asyncio.sleep(0.15)
                    continue
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

        ``config._freeze_only``: skip the heavy PUT (can hit User limit reached)
        and only call /disabled — that is what actually cuts traffic.
        """
        want_disabled = self._want_disabled(payload)
        cfg = payload.config or {}
        hint = cfg.get("_remote_id") or cfg.get("remote_id")
        freeze_only = bool(cfg.get("_freeze_only")) and want_disabled
        mod = AdapterResult(success=False, error="modify not attempted")
        group_ids: list[int] = []
        if not freeze_only:
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
                # Remote already gone — treat as successful freeze so Astra
                # marks ASTRA_QUOTA_FROZEN and stops retrying forever.
                return AdapterResult(
                    success=True,
                    remote_username=payload.username,
                    raw={"astra_ghost_frozen": True, "mod_error": mod.error},
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
            err_l = (dis.error or "").lower()
            if any(
                x in err_l
                for x in (
                    "not found",
                    "یافت نشد",
                    "پیدا نشد",
                    "وجود ندارد",
                    "does not exist",
                )
            ):
                return AdapterResult(
                    success=True,
                    remote_username=payload.username,
                    remote_id=str(uid),
                    raw={"astra_ghost_frozen": True, "disable_error": dis.error},
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
