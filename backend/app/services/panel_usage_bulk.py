"""Bulk remote usage sync — one HTTP list updates thousands of accounts.

Marzban / PasarGuard / Rebecca expose GET /api/users. Pulling every Astra
user one-by-one cannot scale; this keeps quota accurate for large installs.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.adapters import get_adapter
from app.adapters.marzban import MarzbanAdapter
from app.core.database import AsyncSessionLocal
from app.models.panel import Panel, PanelType
from app.models.user import PanelAccount, PanelAccountStatus, User, UserStatus

logger = logging.getLogger("astra.usage")

# foo_g12 / foo_p3 / foo_p3a1 — Astra aliases on shared panels
_ALIAS_RE = re.compile(
    r"^(?P<base>.+?)_(?P<kind>[gp])(?P<num>\d+)(?P<extra>[0-9a-fA-F]*)?$",
    re.IGNORECASE,
)

_BULK_TYPES = {
    PanelType.pasarguard,
    PanelType.marzban,
    PanelType.rebecca,
    PanelType.sanaei,
}
_ROTATE_CURSOR = 0

# Set on panel_accounts after a successful remote quota freeze.
QUOTA_FROZEN_MARK = "ASTRA_QUOTA_FROZEN"


def _worker_owns_bulk(panel: Panel) -> bool:
    """True when a healthy Worker agent already collects this panel's meters."""
    wid = getattr(panel, "worker_id", None)
    if not wid:
        return False
    try:
        from app.services.worker_client import worker_health_cached

        ok, _ = worker_health_cached(int(wid))
        return ok is True
    except Exception:  # noqa: BLE001
        return False


def _bulk_local_adapter(adapter: Any) -> Any:
    """Unwrap WorkerRoutedAdapter so isinstance(_authed) works on failover."""
    return getattr(adapter, "_local", adapter)


async def _close_adapter(adapter: Any) -> None:
    close = getattr(adapter, "close", None)
    if callable(close):
        try:
            await close()
        except Exception:  # noqa: BLE001
            pass


def _as_int(raw: object) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def parse_user_used_traffic(data: dict[str, Any]) -> int:
    """Current-period used bytes (Marzban / PasarGuard / Rebecca).

    Prefer ``used_traffic``. Some PasarGuard list payloads omit the field
    while ``lifetime_used_traffic`` still reflects consumption — use lifetime
    **only when the period key is absent**. If ``used_traffic`` is present and
    ``0`` (legit reset), never fall through to lifetime (that inflated quotas
    and falsely exhausted users).
    """
    if "used_traffic" in data:
        used = _as_int(data.get("used_traffic"))
        if used is not None:
            return used
    for key in ("current_usage", "total_usage"):
        got = _as_int(data.get(key))
        if got is not None and got > 0:
            return got
    traffic = data.get("traffic")
    if isinstance(traffic, dict):
        for key in ("used", "used_traffic", "total"):
            got = _as_int(traffic.get(key))
            if got is not None and got > 0:
                return got
        try:
            up = int(traffic.get("upload") or traffic.get("up") or 0)
            down = int(traffic.get("download") or traffic.get("down") or 0)
            if up or down:
                return max(0, up + down)
        except (TypeError, ValueError):
            pass
    # Period counter missing entirely — last-resort fallback only.
    for key in ("lifetime_used_traffic", "total_traffic"):
        got = _as_int(data.get(key))
        if got is not None and got > 0:
            return got
    return 0


async def fetch_sanaei_usage_map(panel: Panel) -> dict[str, int]:
    """email → used bytes from one Sanaei ``inbounds/list`` (all clients)."""
    from app.adapters.sanaei import SanaeiAdapter

    if _worker_owns_bulk(panel):
        # Healthy worker collect already reports meters — avoid double HTTP.
        return {}
    adapter = get_adapter(panel)
    local = _bulk_local_adapter(adapter)
    if not isinstance(local, SanaeiAdapter):
        await _close_adapter(adapter)
        return {}
    out: dict[str, int] = {}
    try:
        inbounds = await local._raw_inbounds_for_usage()
        for inbound in inbounds or []:
            if not isinstance(inbound, dict):
                continue
            stats = inbound.get("clientStats") or []
            hit = False
            if isinstance(stats, list):
                for stat in stats:
                    if not isinstance(stat, dict):
                        continue
                    email = str(stat.get("email") or "").strip()
                    if not email:
                        continue
                    hit = True
                    got = int(stat.get("up") or 0) + int(stat.get("down") or 0)
                    out[email] = int(out.get(email) or 0) + max(0, got)
            if hit:
                continue
            settings_raw = inbound.get("settings")
            settings: Any = settings_raw
            if isinstance(settings_raw, str):
                try:
                    import json as _json

                    settings = _json.loads(settings_raw)
                except Exception:  # noqa: BLE001
                    settings = {}
            if not isinstance(settings, dict):
                continue
            for client in settings.get("clients") or []:
                if not isinstance(client, dict):
                    continue
                email = str(client.get("email") or "").strip()
                if not email:
                    continue
                got = int(client.get("up") or 0) + int(client.get("down") or 0)
                out[email] = int(out.get(email) or 0) + max(0, got)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sanaei bulk usage failed panel=%s: %s", panel.id, exc)
    finally:
        await _close_adapter(adapter)
    return out


async def fetch_panel_usage_map(
    panel: Panel, *, max_pages: int = 250, page_size: int = 200
) -> dict[str, int]:
    """username → used_traffic bytes via GET /api/users (paginated)."""
    if panel.type not in _BULK_TYPES:
        return {}
    if _worker_owns_bulk(panel):
        # Worker agent owns full concurrent collect for assigned healthy panels.
        return {}
    if panel.type == PanelType.sanaei:
        return await fetch_sanaei_usage_map(panel)
    adapter = get_adapter(panel)
    local = _bulk_local_adapter(adapter)
    if not isinstance(local, MarzbanAdapter):
        await _close_adapter(adapter)
        return {}

    out: dict[str, int] = {}
    try:
        client, headers = await local._authed()
        timeout = httpx.Timeout(25.0, connect=8.0)
        offset = 0
        for _ in range(max(1, int(max_pages))):
            resp = await client.get(
                "/api/users",
                headers=headers,
                params={"limit": int(page_size), "offset": int(offset)},
                timeout=timeout,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "bulk users HTTP %s panel=%s", resp.status_code, panel.id
                )
                break
            try:
                payload = resp.json()
            except Exception:  # noqa: BLE001
                break
            users = []
            total_hint = None
            if isinstance(payload, dict):
                users = payload.get("users") or payload.get("items") or []
                for tk in ("total", "total_users", "count"):
                    if payload.get(tk) is not None:
                        try:
                            total_hint = int(payload.get(tk))
                        except (TypeError, ValueError):
                            total_hint = None
                        break
            elif isinstance(payload, list):
                users = payload
            if not isinstance(users, list) or not users:
                break
            for row in users:
                if not isinstance(row, dict):
                    continue
                uname = str(row.get("username") or "").strip()
                used = parse_user_used_traffic(row)
                if uname:
                    # Keep the larger reading if the same name appears twice
                    out[uname] = max(int(out.get(uname) or 0), used)
                # PasarGuard also keys remotes by numeric id in remote_id
                uid = row.get("id")
                if uid is not None and str(uid).strip():
                    kid = str(uid).strip()
                    out[kid] = max(int(out.get(kid) or 0), used)
            offset += len(users)
            if len(users) < int(page_size):
                break
            if total_hint is not None and offset >= int(total_hint):
                break
            await asyncio.sleep(0.02)
    except Exception as exc:  # noqa: BLE001
        logger.warning("bulk users failed panel=%s: %s", panel.id, exc)
    finally:
        await _close_adapter(adapter)
    return out


def _build_usage_indexes(
    usage_map: dict[str, int],
) -> tuple[dict[str, int], dict[str, int], dict[tuple[str, str, int], int]]:
    """exact, lower, and (base, g|p, num) alias indexes."""
    usage_exact = {
        str(k).strip(): int(v) for k, v in usage_map.items() if str(k).strip()
    }
    usage_lower = {k.lower(): v for k, v in usage_exact.items()}
    alias_idx: dict[tuple[str, str, int], int] = {}
    for name, used in usage_exact.items():
        m = _ALIAS_RE.match(name)
        if not m:
            continue
        base = (m.group("base") or "").strip().lower()
        kind = (m.group("kind") or "").lower()
        try:
            num = int(m.group("num") or 0)
        except (TypeError, ValueError):
            continue
        if not base or kind not in ("g", "p") or num <= 0:
            continue
        key = (base, kind, num)
        prev = alias_idx.get(key)
        alias_idx[key] = used if prev is None else max(prev, used)
    return usage_exact, usage_lower, alias_idx


def _lookup_usage_bytes(
    usage_exact: dict[str, int],
    usage_lower: dict[str, int],
    alias_idx: dict[tuple[str, str, int], int],
    *,
    astra_username: str,
    remote_username: str | None,
    remote_id: str | None,
    panel_id: int,
    group_id: int | None,
) -> int | None:
    """Match remote meters across base / ``_g`` / ``_p`` aliases."""
    from app.services.orchestrator import (
        _remote_username_candidates,
        sanitize_remote_username,
    )

    names = _remote_username_candidates(
        astra_username=astra_username or "",
        remote_username=remote_username,
        remote_id=remote_id,
        uuid=None,
        panel_id=int(panel_id),
        group_id=int(group_id) if group_id is not None else None,
    )
    for name in names:
        if name in usage_exact:
            return int(usage_exact[name])
        hit = usage_lower.get(name.lower())
        if hit is not None:
            return int(hit)

    base = sanitize_remote_username(astra_username or "").lower()
    if not base:
        return None
    if group_id is not None:
        hit = alias_idx.get((base, "g", int(group_id)))
        if hit is not None:
            return int(hit)
    hit = alias_idx.get((base, "p", int(panel_id)))
    if hit is not None:
        return int(hit)
    return None


# Ignore "used=0 / tiny" API snapshots that used to collapse last_remote_raw
# and re-add the full counter on the next good reading (exact N× inflation).
_METER_BLIP_MAX = 1 * 1024 * 1024  # 1 MiB
_METER_JITTER_KEEP = 0.95  # drops within 5% of watermark = jitter, not reset


def _high_water_delta(
    acc: PanelAccount,
    got: int,
    *,
    allow_shrink: bool = False,
) -> tuple[int, int, bool]:
    """Return ``(raw_delta, new_accumulated, touch_last)``.

    ``panel_accounts.used_traffic`` is accumulated Astra raw (survives remote
    resets). ``last_remote_raw`` tracks the last observed remote counter.

    ``touch_last=False`` means the reading is a blip — keep the watermark.
    """
    got_i = max(0, int(got or 0))
    accumulated = int(acc.used_traffic or 0)
    last = getattr(acc, "last_remote_raw", None)
    if allow_shrink:
        # Admin reset path: absolute set, no accumulation.
        return 0, got_i, True
    if last is None:
        # Not primed yet — stamp watermark without inventing a delta
        # (startup backfill normally sets last = used_traffic before this runs).
        return 0, accumulated, True
    last_i = int(last or 0)
    if got_i >= last_i:
        raw_delta = got_i - last_i
        return max(0, int(raw_delta)), accumulated + max(0, int(raw_delta)), True

    # --- got < last: blip vs real remote reset ---
    # Zero / near-zero while watermark is healthy: NEVER collapse last_remote_raw.
    # Real resets are detected when usage climbs again from a non-trivial value
    # below the old watermark (got > blip ceiling).
    if last_i > 0 and (
        got_i == 0
        or (got_i <= _METER_BLIP_MAX and last_i > _METER_BLIP_MAX * 4)
        or got_i >= int(last_i * _METER_JITTER_KEEP)
    ):
        return 0, accumulated, False

    # Confirmed remote wipe / period reset — keep prior accumulated, count new.
    raw_delta = got_i
    return max(0, int(raw_delta)), accumulated + max(0, int(raw_delta)), True


def _account_lookup_keys(
    *,
    astra_username: str,
    remote_username: str | None,
    remote_id: str | None,
    panel_id: int,
    group_id: int | None,
) -> list[str]:
    """All O(1) keys that should resolve to this account."""
    from app.services.orchestrator import _remote_username_candidates

    keys = _remote_username_candidates(
        astra_username=astra_username or "",
        remote_username=remote_username,
        remote_id=remote_id,
        uuid=None,
        panel_id=int(panel_id),
        group_id=int(group_id) if group_id is not None else None,
    )
    out: list[str] = []
    seen: set[str] = set()
    for k in keys:
        low = (k or "").strip().lower()
        if low and low not in seen:
            seen.add(low)
            out.append(low)
    rid = (remote_id or "").strip()
    if rid and rid.isdigit() and rid not in seen:
        out.append(rid)
    return out


def _apply_got_to_account(
    acc: PanelAccount,
    got: int,
    *,
    now: datetime,
    dirty: set[int],
    allow_shrink: bool = False,
    coeff: float = 1.0,
    user_weighted_deltas: dict[int, int] | None = None,
) -> int:
    """Apply one remote reading via high-water mark. Returns weighted_delta."""
    from app.services.orchestrator import _norm_coeff

    got_i = max(0, int(got or 0))
    raw_delta, new_acc, touch_last = _high_water_delta(
        acc, got_i, allow_shrink=allow_shrink
    )
    c = _norm_coeff(
        getattr(acc, "applied_coefficient", None)
        if getattr(acc, "applied_coefficient", None) is not None
        else coeff
    )
    if getattr(acc, "applied_coefficient", None) is None:
        acc.applied_coefficient = c

    if allow_shrink:
        acc.used_traffic = got_i
        acc.last_remote_raw = got_i
        acc.coeff_anchor_raw = 0
        acc.coeff_anchor_weighted = 0
        acc.last_sync_at = now
        dirty.add(int(acc.user_id))
        return 0

    # Blip / jitter: refresh sync stamp only — never lower the watermark.
    if not touch_last:
        acc.last_sync_at = now
        return 0

    if raw_delta == 0 and int(getattr(acc, "last_remote_raw", None) or -1) == got_i:
        acc.last_sync_at = now
        return 0

    weighted_delta = int(round(raw_delta * c)) if raw_delta else 0
    acc.used_traffic = int(new_acc)
    acc.last_remote_raw = got_i
    acc.last_sync_at = now
    # Keep ASTRA_QUOTA_FROZEN sticky — clearing it on every meter tick made
    # heal re-enqueue KILL_USER forever and trimmed newer kills out of Redis.
    if acc.status == PanelAccountStatus.error:
        if (acc.error_message or "") != QUOTA_FROZEN_MARK:
            acc.status = PanelAccountStatus.active
            acc.error_message = None
    # Pending rows that already hold a real meter are live — finish activation.
    if (
        acc.status == PanelAccountStatus.pending
        and int(acc.used_traffic or 0) > 0
    ):
        acc.status = PanelAccountStatus.active
        if (acc.error_message or "") != QUOTA_FROZEN_MARK:
            acc.error_message = None
    if raw_delta > 0 or weighted_delta > 0:
        dirty.add(int(acc.user_id))
        if user_weighted_deltas is not None and weighted_delta:
            uid = int(acc.user_id)
            user_weighted_deltas[uid] = (
                int(user_weighted_deltas.get(uid, 0)) + weighted_delta
            )
    return weighted_delta


async def apply_usage_map_to_panel(
    db: AsyncSession,
    panel_id: int,
    usage_map: dict[str, int],
    *,
    allow_shrink: bool = False,
) -> set[int]:
    """High-water delta apply + period-locked user increment. Returns dirty ids.

    Builds an O(1) lookup of remote_username / aliases → account, then for each
    meter computes ``raw_delta`` against ``last_remote_raw`` and atomically
    increments ``users.used_traffic`` by ``round(raw_delta × coeff)``.
    """
    if not usage_map:
        return set()
    usage_exact = {
        str(k).strip(): max(0, int(v or 0))
        for k, v in usage_map.items()
        if str(k).strip()
    }
    if not usage_exact:
        return set()

    name_keys = list(usage_exact.keys())
    base_names: set[str] = set()
    for n in name_keys:
        base_names.add(n)
        low = n.lower()
        for sep in ("_g", "_p"):
            idx = low.rfind(sep)
            if idx > 0 and low[idx + 2 :].isdigit():
                base_names.add(n[:idx])
                break
    from sqlalchemy import or_

    # Pending-with-usage must receive the same remote meter (provision lag).
    status_ok = PanelAccount.status.in_(
        (
            PanelAccountStatus.active,
            PanelAccountStatus.error,
            PanelAccountStatus.pending,
        )
    )
    user_ok = User.status.in_(
        (UserStatus.active, UserStatus.on_hold, UserStatus.limited)
    )
    # Always case-insensitive — worker cache sync lowercases remotes; PG IN is
    # case-sensitive and previously dropped mixed-case panel_accounts so meters
    # never applied (silent under-billing / stale rows).
    from sqlalchemy import func

    if len(name_keys) <= 4000 and name_keys:
        rows: list = []
        chunk_sz = 900
        keys = list(
            dict.fromkeys(
                str(k).strip().lower()
                for k in (name_keys + list(base_names))
                if str(k).strip()
            )
        )
        for off in range(0, len(keys), chunk_sz):
            chunk = keys[off : off + chunk_sz]
            result = await db.execute(
                select(PanelAccount, User.username)
                .join(User, User.id == PanelAccount.user_id)
                .where(
                    PanelAccount.panel_id == int(panel_id),
                    status_ok,
                    user_ok,
                    or_(
                        func.lower(PanelAccount.remote_username).in_(chunk),
                        func.lower(User.username).in_(chunk),
                    ),
                )
            )
            rows.extend(result.all())
        seen: set[int] = set()
        uniq = []
        for acc, uname in rows:
            aid = int(acc.id)
            if aid in seen:
                continue
            seen.add(aid)
            uniq.append((acc, uname))
        rows = uniq
    else:
        result = await db.execute(
            select(PanelAccount, User.username)
            .join(User, User.id == PanelAccount.user_id)
            .where(
                PanelAccount.panel_id == int(panel_id),
                status_ok,
                user_ok,
            )
        )
        rows = list(result.all())

    # Live group coefficients for this panel (period-lock on change).
    from app.services.orchestrator import (
        _norm_coeff,
        apply_coefficient_period,
        coefficient_from_config,
    )

    coeff_by_group: dict[int, float] = {}
    try:
        from app.models.group import GroupPanel

        gp_rows = (
            await db.execute(
                select(GroupPanel).where(GroupPanel.panel_id == int(panel_id))
            )
        ).scalars().all()
        for link in gp_rows:
            cfg = link.config if isinstance(link.config, dict) else {}
            coeff_by_group[int(link.group_id)] = _norm_coeff(
                coefficient_from_config(cfg)
            )
    except Exception:  # noqa: BLE001
        coeff_by_group = {}

    # O(1) lookup: normalized remote key → PanelAccount
    lookup: dict[str, PanelAccount] = {}
    for acc, astra_uname in rows:
        gid = int(acc.group_id) if getattr(acc, "group_id", None) is not None else None
        live_c = coeff_by_group.get(gid, 1.0) if gid is not None else 1.0
        apply_coefficient_period(acc, live_c)
        for key in _account_lookup_keys(
            astra_username=str(astra_uname or ""),
            remote_username=(acc.remote_username or "").strip() or None,
            remote_id=str(acc.remote_id).strip()
            if acc.remote_id and not str(acc.remote_id).startswith("{")
            else None,
            panel_id=int(panel_id),
            group_id=gid,
        ):
            # Prefer the account that already owns this exact remote_username.
            if key not in lookup:
                lookup[key] = acc
            else:
                existing = lookup[key]
                rem = (acc.remote_username or "").strip().lower()
                if rem == key:
                    lookup[key] = acc
                elif (existing.remote_username or "").strip().lower() != key:
                    lookup[key] = acc

    now = datetime.now(timezone.utc)
    dirty: set[int] = set()
    user_weighted: dict[int, int] = {}
    # (account_id, new_used, last_remote_raw)
    bulk_rows: list[tuple[int, int, int]] = []
    # Matched but unchanged — still stamp last_sync_at for fleet freshness.
    sync_touch_ids: list[int] = []
    matched = 0
    seen_acc: set[int] = set()

    # Index siblings sharing the same remote on this panel so one meter
    # updates every group row (weighted path dedupes; freshness needs all).
    siblings_by_remote: dict[str, list[PanelAccount]] = {}
    for acc, _uname in rows:
        rem = (acc.remote_username or "").strip().lower()
        if rem:
            siblings_by_remote.setdefault(rem, []).append(acc)

    for i, (name, got_raw) in enumerate(usage_exact.items()):
        acc = lookup.get(name.lower()) or lookup.get(name)
        if acc is None:
            continue
        aid = int(acc.id)
        if aid in seen_acc:
            # Same account matched under multiple meter keys — keep max got.
            # Re-apply with the larger reading only.
            prev_got = int(getattr(acc, "last_remote_raw", None) or 0)
            if int(got_raw) <= prev_got and not allow_shrink:
                continue
        targets = [acc]
        rem_key = (acc.remote_username or "").strip().lower() or name.lower()
        for sib in siblings_by_remote.get(rem_key, []):
            if int(sib.id) != aid:
                targets.append(sib)
        got = int(got_raw)
        for ti, tgt in enumerate(targets):
            tid = int(tgt.id)
            if tid in seen_acc and tid != aid:
                # Already processed via another meter key this wave.
                continue
            seen_acc.add(tid)
            matched += 1
            c = _norm_coeff(getattr(tgt, "applied_coefficient", None) or 1.0)
            before_used = int(tgt.used_traffic or 0)
            before_last = getattr(tgt, "last_remote_raw", None)
            # Sibling rows share one remote counter — sync their raw watermark,
            # but bump users.used_traffic only once (primary match).
            _apply_got_to_account(
                tgt,
                got,
                now=now,
                dirty=dirty,
                allow_shrink=allow_shrink,
                coeff=c,
                user_weighted_deltas=user_weighted if ti == 0 else None,
            )
            if allow_shrink or int(tgt.used_traffic or 0) != before_used or (
                getattr(tgt, "last_remote_raw", None) != before_last
            ):
                bulk_rows.append(
                    (
                        tid,
                        int(tgt.used_traffic or 0),
                        int(tgt.last_remote_raw or 0),
                    )
                )
            else:
                sync_touch_ids.append(tid)
        if i and i % 500 == 0:
            await asyncio.sleep(0)

    if bulk_rows:
        from sqlalchemy import text

        for off in range(0, len(bulk_rows), 800):
            chunk = bulk_rows[off : off + 800]
            values_sql = ", ".join(
                f"({aid}::int, {used}::bigint, {last}::bigint)"
                for aid, used, last in chunk
            )
            await db.execute(
                text(
                    f"""
                    UPDATE panel_accounts AS pa
                    SET used_traffic = v.used,
                        last_remote_raw = v.last,
                        last_sync_at = :now,
                        status = CASE
                            -- Keep ASTRA_QUOTA_FROZEN sticky (do not revive error
                            -- rows that are mid-kill); only promote pending→active.
                            WHEN pa.error_message = :frozen THEN pa.status
                            WHEN pa.status = 'error' THEN 'active'
                            WHEN pa.status = 'pending' AND v.used > 0 THEN 'active'
                            ELSE pa.status
                        END
                    FROM (VALUES {values_sql}) AS v(id, used, last)
                    WHERE pa.id = v.id
                    """
                ),
                {"now": now, "frozen": QUOTA_FROZEN_MARK},
            )

    if sync_touch_ids:
        from sqlalchemy import text

        uniq_touch = sorted({int(x) for x in sync_touch_ids})
        for off in range(0, len(uniq_touch), 900):
            chunk = uniq_touch[off : off + 900]
            ids_sql = ", ".join(str(i) for i in chunk)
            await db.execute(
                text(
                    f"""
                    UPDATE panel_accounts
                    SET last_sync_at = :now
                    WHERE id IN ({ids_sql})
                    """
                ),
                {"now": now},
            )

    # Atomically bump users.used_traffic by period-locked weighted deltas.
    if user_weighted and not allow_shrink:
        from sqlalchemy import text

        items = [(uid, int(d)) for uid, d in user_weighted.items() if int(d) > 0]
        for off in range(0, len(items), 500):
            chunk = items[off : off + 500]
            values_sql = ", ".join(
                f"({uid}::int, {delta}::bigint)" for uid, delta in chunk
            )
            await db.execute(
                text(
                    f"""
                    UPDATE users AS u
                    SET used_traffic = u.used_traffic + v.delta
                    FROM (VALUES {values_sql}) AS v(id, delta)
                    WHERE u.id = v.id
                    """
                )
            )
        # Feed analytics hour buckets from the same known deltas (isolated txn).
        if items:
            try:
                from app.services import user_analytics as ua

                await ua.record_usage_deltas(dict(items))
            except Exception:  # noqa: BLE001
                logger.debug("analytics delta record failed", exc_info=True)

    if rows and matched < max(1, len(rows) // 4) and len(usage_exact) > 20:
        logger.warning(
            "bulk match sparse panel=%s accounts=%s matched=%s map=%s dirty=%s",
            panel_id,
            len(rows),
            matched,
            len(usage_exact),
            len(dirty),
        )
    return dirty


async def heal_zero_accounts_via_get_usage(
    panel: Panel,
    *,
    limit: int = 400,
    concurrency: int = 20,
) -> set[int]:
    """Per-remote get_usage for accounts still at 0 after bulk map miss.

    Covers PasarGuard/Marzban aliases that the list map did not match
    (``_g`` / ``_p`` / empty remote_username).
    """
    from app.services.orchestrator import _remote_username_candidates

    if panel.type not in (
        PanelType.pasarguard,
        PanelType.marzban,
        PanelType.rebecca,
        PanelType.sanaei,
    ):
        return set()
    pid = int(panel.id)
    # Prefer never-synced / unknown-remote first (most likely alias miss).
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(PanelAccount, User.username)
                .join(User, User.id == PanelAccount.user_id)
                .where(
                    PanelAccount.panel_id == pid,
                    PanelAccount.status.in_(
                        (
                            PanelAccountStatus.active,
                            PanelAccountStatus.error,
                        )
                    ),
                    PanelAccount.used_traffic <= 0,
                )
                .order_by(
                    PanelAccount.last_sync_at.asc().nullsfirst(),
                    PanelAccount.id.desc(),
                )
                .limit(max(1, int(limit)))
            )
        ).all()
        # Touch panel attrs for detached get_adapter use (incl. worker route)
        _ = (
            panel.id,
            panel.type,
            panel.name,
            panel.is_active,
            getattr(panel, "base_url", None),
            getattr(panel, "url", None),
            getattr(panel, "username", None),
            getattr(panel, "password", None),
            getattr(panel, "api_token", None),
            getattr(panel, "token", None),
            getattr(panel, "worker_id", None),
            getattr(panel, "meta", None),
        )

    if not rows:
        return set()

    jobs: list[tuple[int, int, list[str]]] = []
    for acc, astra_uname in rows:
        names = _remote_username_candidates(
            astra_username=str(astra_uname or ""),
            remote_username=(acc.remote_username or "").strip() or None,
            remote_id=str(acc.remote_id).strip()
            if acc.remote_id and not str(acc.remote_id).startswith("{")
            else None,
            uuid=None,
            panel_id=pid,
            group_id=int(acc.group_id)
            if getattr(acc, "group_id", None) is not None
            else None,
        )
        if not names:
            continue
        jobs.append((int(acc.id), int(acc.user_id), names))

    if not jobs:
        return set()

    sem = asyncio.Semaphore(max(4, int(concurrency)))
    found: dict[int, tuple[int, str]] = {}  # account_id -> (used, remote_name)

    async def _one(aid: int, names: list[str]) -> None:
        async with sem:
            adapter = get_adapter(panel)
            try:
                for name in names:
                    if not name:
                        continue
                    try:
                        try:
                            res = await asyncio.wait_for(
                                adapter.get_usage(  # type: ignore[call-arg]
                                    name, name, expand_links=False
                                ),
                                timeout=8.0,
                            )
                        except TypeError:
                            res = await asyncio.wait_for(
                                adapter.get_usage(name, name),
                                timeout=8.0,
                            )
                    except Exception:  # noqa: BLE001
                        continue
                    if res is not None and res.success:
                        got = int(res.used_traffic or 0)
                        if got > 0:
                            hit_name = (
                                str(res.remote_username or "").strip() or name
                            )
                            found[aid] = (got, hit_name)
                            return
            finally:
                close = getattr(adapter, "close", None)
                if callable(close):
                    try:
                        await close()
                    except Exception:  # noqa: BLE001
                        pass

    await asyncio.gather(
        *[_one(aid, names) for aid, _uid, names in jobs],
        return_exceptions=True,
    )
    if not found:
        return set()

    dirty: set[int] = set()
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        accs = (
            await db.execute(
                select(PanelAccount).where(PanelAccount.id.in_(list(found.keys())))
            )
        ).scalars().all()
        by_id = {int(a.id): a for a in accs}
        for aid, (got, hit_name) in found.items():
            acc = by_id.get(int(aid))
            if acc is None:
                continue
            if hit_name and (acc.remote_username or "").strip() != hit_name:
                acc.remote_username = hit_name
            _apply_got_to_account(acc, int(got), now=now, dirty=dirty)
        await db.commit()
    logger.info(
        "heal-zero panel=%s tried=%s found=%s dirty=%s",
        pid,
        len(jobs),
        len(found),
        len(dirty),
    )
    return dirty


async def _load_user_for_recount(
    db, uid: int
) -> tuple[User, list[PanelAccount]] | tuple[None, list[PanelAccount]]:
    """Load one user + accounts for recount — sequential queries only.

    Never uses sibling/nested ``selectinload`` (asyncpg forbids concurrent ops
    on one connection). Panels are attached via ``set_committed_value`` so
    commit/flush cannot trigger a lazy load race.
    """
    from sqlalchemy.orm.attributes import set_committed_value

    user = (
        await db.execute(select(User).where(User.id == int(uid)))
    ).scalar_one_or_none()
    if user is None:
        return None, []
    accounts = list(
        (
            await db.execute(
                select(PanelAccount).where(PanelAccount.user_id == int(uid))
            )
        )
        .scalars()
        .all()
    )
    if accounts:
        panel_ids = list({int(a.panel_id) for a in accounts if a.panel_id})
        panels_by_id: dict[int, Panel] = {}
        if panel_ids:
            prow = (
                await db.execute(select(Panel).where(Panel.id.in_(panel_ids)))
            ).scalars().all()
            panels_by_id = {int(p.id): p for p in prow}
        for acc in accounts:
            panel = panels_by_id.get(int(acc.panel_id))
            if panel is not None:
                set_committed_value(acc, "panel", panel)
    set_committed_value(user, "panel_accounts", accounts)
    return user, accounts


async def _recount_one_user(uid: int) -> int | None:
    """Isolated-session recount for one user. Returns flipped user_id or None."""
    from app.services.orchestrator import (
        activate_on_hold_if_used,
        enforce_user_quota,
        recount_user_used_traffic,
    )

    last_exc: BaseException | None = None
    for attempt in range(2):
        try:
            async with AsyncSessionLocal() as db:
                user, accounts = await _load_user_for_recount(db, uid)
                if user is None:
                    return None
                if user.status not in (
                    UserStatus.active,
                    UserStatus.on_hold,
                    UserStatus.limited,
                ):
                    return None
                exp = int(getattr(user, "expire", 0) or 0)
                if exp > 0 and exp <= int(time.time()):
                    return None
                await recount_user_used_traffic(db, user, accounts)
                activated = await activate_on_hold_if_used(db, user)
                changed = bool(activated) or await enforce_user_quota(db, user)
                await db.commit()
                if user.status in (
                    UserStatus.limited,
                    UserStatus.expired,
                    UserStatus.disabled,
                ):
                    return int(user.id)
                if changed:
                    return int(user.id)
                return None
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt == 0:
                await asyncio.sleep(0.2)
                continue
            logger.warning(
                "recount failed user_id=%s", uid, exc_info=last_exc
            )
            return None
    return None


async def recount_users_usage(user_ids: list[int], *, concurrency: int = 8) -> list[int]:
    """Recompute weighted used_traffic + enforce quota. Returns flipped user ids.

    Always sequential on one session path via ``_recount_one_user`` (each user
    opens its own ``AsyncSessionLocal``). Concurrent gather on shared/pooled
    checkouts previously caused asyncpg InternalClientError under load; the
    ``concurrency`` / Sem(8) knob is retained for callers but capped to 1.
    """
    ids = list(dict.fromkeys(int(x) for x in user_ids if x))
    if not ids:
        return []
    flipped: list[int] = []
    # Force sequential — isolated sessions under gather still raced the
    # asyncpg pool (dispose / idle-in-txn terminate / selectinload).
    _ = concurrency  # API compat; do not gather
    for uid in ids:
        got = await _recount_one_user(uid)
        if got is not None:
            flipped.append(got)
        await asyncio.sleep(0)
    return list(dict.fromkeys(flipped))


async def sync_one_panel_bulk(
    panel_id: int, *, recount: bool = True, recount_concurrency: int = 24
) -> dict[str, int]:
    """Fetch /api/users for one panel, apply, heal zeros, recount dirty users."""
    t0 = time.monotonic()
    async with AsyncSessionLocal() as db:
        panel = (
            await db.execute(select(Panel).where(Panel.id == int(panel_id)))
        ).scalar_one_or_none()
        if panel is None or not panel.is_active or panel.type not in _BULK_TYPES:
            return {"accounts": 0, "dirty": 0, "flipped": 0, "listed": 0}
        pid = int(panel.id)
        # Keep worker_id / creds after session closes for get_adapter routing.
        _ = (
            panel.id,
            panel.type,
            panel.is_active,
            getattr(panel, "base_url", None),
            getattr(panel, "username", None),
            getattr(panel, "password", None),
            getattr(panel, "api_token", None),
            getattr(panel, "worker_id", None),
            dict(getattr(panel, "meta", None) or {}),
        )
        panel_ref = panel
        if _worker_owns_bulk(panel_ref):
            return {
                "accounts": 0,
                "dirty": 0,
                "flipped": 0,
                "listed": 0,
                "worker_owned": 1,
            }
        from sqlalchemy import func as _func

        acc_n = int(
            (
                await db.execute(
                    select(_func.count())
                    .select_from(PanelAccount)
                    .where(
                        PanelAccount.panel_id == pid,
                        PanelAccount.status.in_(
                            (
                                PanelAccountStatus.active,
                                PanelAccountStatus.error,
                            )
                        ),
                    )
                )
            ).scalar()
            or 0
        )
    usage_map = await fetch_panel_usage_map(panel_ref)
    dirty: set[int] = set()
    matched_dirty = 0
    if usage_map:
        async with AsyncSessionLocal() as db:
            dirty = await apply_usage_map_to_panel(db, pid, usage_map)
            matched_dirty = len(dirty)
            await db.commit()
    # Per-account heal is expensive (1 HTTP each). Only for sparse map / small miss
    # set — unbounded heal was hanging fleet for tens of minutes per panel.
    map_ratio = (len(usage_map) / acc_n) if acc_n > 0 else 0.0
    need_heal = (not usage_map) or map_ratio < 0.35 or matched_dirty < max(1, acc_n // 20)
    if need_heal:
        heal_lim = 60 if panel_ref.type == PanelType.pasarguard else 40
        heal_conc = 8 if panel_ref.type == PanelType.pasarguard else 6
        try:
            healed = await asyncio.wait_for(
                heal_zero_accounts_via_get_usage(
                    panel_ref,
                    limit=heal_lim,
                    concurrency=heal_conc,
                ),
                timeout=45.0,
            )
            dirty |= healed
        except asyncio.TimeoutError:
            logger.warning("heal-zero panel=%s timed out", pid)
        except Exception:  # noqa: BLE001
            logger.warning("heal-zero panel=%s failed", pid, exc_info=True)

    if not usage_map and not dirty:
        return {"accounts": 0, "dirty": 0, "flipped": 0, "listed": 0}

    recount_ids = list(dict.fromkeys(int(x) for x in dirty))
    flipped: list[int] = []
    if recount and recount_ids:
        # Cap hard — high concurrency was exhausting QueuePool and freezing
        # live meters for consuming users while fleet recounted hundreds.
        conc = max(2, min(4, int(recount_concurrency)))
        # Prefer largest deltas first; defer the long tail to the next rotate.
        if len(recount_ids) > 80:
            recount_ids = recount_ids[:80]
        flipped = await recount_users_usage(recount_ids, concurrency=conc)
    try:
        from app.services import user_analytics as analytics_svc

        await analytics_svc.align_analytics_cursors(list(dirty))
    except Exception:  # noqa: BLE001
        logger.debug("analytics cursor align after bulk failed", exc_info=True)
    dt = time.monotonic() - t0
    logger.info(
        "bulk usage panel=%s listed=%s dirty=%s recount=%s flipped=%s dt=%.1fs",
        pid,
        len(usage_map),
        len(dirty),
        len(recount_ids) if recount else 0,
        len(flipped),
        dt,
    )
    return {
        "listed": len(usage_map),
        "dirty": len(dirty),
        "flipped": len(flipped),
        "dirty_ids": recount_ids,
        "panel_id": pid,
    }


async def sync_panels_bulk_rotate(*, max_panels: int = 3) -> dict[str, int]:
    """Round-robin bulk sync across Marzban-family panels.

    Skips panels owned by a healthy Worker (agent collect reports meters).
    When the worker is down/unknown, those panels stay in rotation (failover).
    """
    global _ROTATE_CURSOR
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Panel.id, Panel.worker_id)
                .where(
                    Panel.is_active.is_(True),
                    Panel.type.in_(tuple(_BULK_TYPES)),
                )
                .order_by(Panel.id.asc())
            )
        ).all()
    ids: list[int] = []
    for pid, wid in rows:
        if wid:
            try:
                from app.services.worker_client import worker_health_cached

                ok, _ = worker_health_cached(int(wid))
                if ok is True:
                    continue
            except Exception:  # noqa: BLE001
                pass
        ids.append(int(pid))
    if not ids:
        return {"panels": 0, "dirty": 0, "flipped": 0, "listed": 0, "skipped_worker": 1}

    n = max(1, min(int(max_panels), len(ids)))
    start = _ROTATE_CURSOR % len(ids)
    _ROTATE_CURSOR = (start + n) % len(ids)
    chosen = [ids[(start + i) % len(ids)] for i in range(n)]

    listed = dirty = flipped = 0
    for pid in chosen:
        try:
            stats = await sync_one_panel_bulk(pid)
            listed += int(stats.get("listed") or 0)
            dirty += int(stats.get("dirty") or 0)
            flipped += int(stats.get("flipped") or 0)
        except Exception:  # noqa: BLE001
            logger.warning("bulk rotate panel=%s failed", pid, exc_info=True)
    return {
        "panels": len(chosen),
        "listed": listed,
        "dirty": dirty,
        "flipped": flipped,
    }


async def sync_all_panels_bulk(
    *,
    concurrency: int = 10,
    recount: bool = True,
    recount_concurrency: int = 28,
) -> dict[str, int]:
    """Pull usage from EVERY active Marzban-family panel in parallel.

    Healthy worker-owned panels are skipped (agent reports meters).
    """
    t0 = time.monotonic()
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Panel.id, Panel.worker_id)
                .where(
                    Panel.is_active.is_(True),
                    Panel.type.in_(tuple(_BULK_TYPES)),
                )
                .order_by(Panel.id.asc())
            )
        ).all()
    ids: list[int] = []
    for pid, wid in rows:
        if wid:
            try:
                from app.services.worker_client import worker_health_cached

                ok, _ = worker_health_cached(int(wid))
                if ok is True:
                    continue
            except Exception:  # noqa: BLE001
                pass
        ids.append(int(pid))
    if not ids:
        return {"panels": 0, "dirty": 0, "flipped": 0, "listed": 0, "dt": 0}

    sem = asyncio.Semaphore(max(1, int(concurrency)))
    listed = 0
    dirty_ids: set[int] = set()
    lock = asyncio.Lock()

    async def _one(pid: int) -> None:
        nonlocal listed
        async with sem:
            try:
                stats = await sync_one_panel_bulk(
                    pid, recount=False, recount_concurrency=recount_concurrency
                )
            except Exception:  # noqa: BLE001
                logger.warning("fleet bulk panel=%s failed", pid, exc_info=True)
                return
            async with lock:
                listed += int(stats.get("listed") or 0)
                for uid in stats.get("dirty_ids") or []:
                    dirty_ids.add(int(uid))

    await asyncio.gather(*[_one(pid) for pid in ids])

    flipped: list[int] = []
    if recount and dirty_ids:
        ordered = list(dirty_ids)
        chunk = 800
        for i in range(0, len(ordered), chunk):
            part = await recount_users_usage(
                ordered[i : i + chunk],
                concurrency=max(8, int(recount_concurrency)),
            )
            flipped.extend(part)

    dt = time.monotonic() - t0
    logger.info(
        "fleet bulk panels=%s listed=%s dirty=%s flipped=%s dt=%.1fs",
        len(ids),
        listed,
        len(dirty_ids),
        len(flipped),
        dt,
    )
    return {
        "panels": len(ids),
        "listed": listed,
        "dirty": len(dirty_ids),
        "flipped": len(list(dict.fromkeys(flipped))),
        "dt": int(dt),
    }


async def recount_all_live_users(
    *, concurrency: int = 28, chunk: int = 1000
) -> dict[str, int]:
    """Recompute weighted used_traffic for live users only (DB only).

    Skips expired/disabled — they no longer need background usage work.
    """
    from sqlalchemy import or_

    t0 = time.monotonic()
    now_ts = int(time.time())
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(User.id).where(
                    User.status.in_(
                        (
                            UserStatus.active,
                            UserStatus.on_hold,
                            UserStatus.limited,
                        )
                    ),
                    or_(
                        User.expire == 0,
                        User.expire.is_(None),
                        User.expire > now_ts,
                    ),
                )
            )
        ).all()
    ids = [int(r[0]) for r in rows]
    flipped: list[int] = []
    step = max(100, int(chunk))
    for i in range(0, len(ids), step):
        flipped.extend(
            await recount_users_usage(
                ids[i : i + step], concurrency=max(8, int(concurrency))
            )
        )
    dt = time.monotonic() - t0
    logger.info(
        "fleet recount users=%s flipped=%s dt=%.1fs",
        len(ids),
        len(list(dict.fromkeys(flipped))),
        dt,
    )
    return {
        "users": len(ids),
        "flipped": len(list(dict.fromkeys(flipped))),
        "dt": int(dt),
    }


_FLEET_CYCLE_N = 0


async def find_lagging_usage_user_ids(*, limit: int = 800) -> list[int]:
    """Users whose live panel meters already exceed stored used_traffic (DB only).

    Deleted rows are ignored — migrations inflate raw Σ and falsely look like
    lag forever when weighted ``used_traffic`` (with coeffs) is already correct.
    Also catch used=0 while live meters exist.
    """
    from sqlalchemy import text

    lim = max(1, min(5000, int(limit)))
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                text(
                    """
                    SELECT u.id
                    FROM users u
                    JOIN panel_accounts pa ON pa.user_id = u.id
                      AND pa.status IN ('active', 'error', 'pending')
                    WHERE u.status IN ('active', 'on_hold', 'limited')
                    GROUP BY u.id, u.used_traffic
                    HAVING (
                        coalesce(
                          sum(
                            pa.used_traffic
                            * coalesce(pa.applied_coefficient, 1.0)
                          ),
                          0
                        )
                          > coalesce(u.used_traffic, 0) + (20 * 1024 * 1024)
                      )
                      OR (
                        coalesce(u.used_traffic, 0) = 0
                        AND coalesce(sum(pa.used_traffic), 0) > (2 * 1024 * 1024)
                      )
                    ORDER BY (
                      coalesce(
                        sum(
                          pa.used_traffic
                          * coalesce(pa.applied_coefficient, 1.0)
                        ),
                        0
                      ) - coalesce(u.used_traffic, 0)
                    ) DESC
                    LIMIT :lim
                    """
                ),
                {"lim": lim},
            )
        ).all()
    return [int(r[0]) for r in rows]


async def find_stale_sync_user_ids(
    *,
    older_than_sec: int = 90,
    limit: int = 24,
) -> list[int]:
    """Active users whose panel meters have not been pulled recently.

    Fleet rotate can leave a panel for several minutes; these ids must be
    live-pulled so consuming clients never sit on a frozen used_traffic.
    """
    from sqlalchemy import text

    age = max(30, int(older_than_sec))
    lim = max(1, min(200, int(limit)))
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                text(
                    """
                    SELECT u.id
                    FROM users u
                    JOIN panel_accounts pa ON pa.user_id = u.id
                      AND pa.status IN ('active', 'error')
                    WHERE u.status IN ('active', 'on_hold', 'limited')
                    GROUP BY u.id
                    HAVING
                      min(pa.last_sync_at) IS NULL
                      OR min(pa.last_sync_at)
                           < now() - make_interval(secs => :age)
                    ORDER BY min(pa.last_sync_at) NULLS FIRST
                    LIMIT :lim
                    """
                ),
                {"age": age, "lim": lim},
            )
        ).all()
        await db.rollback()
    return [int(r[0]) for r in rows]


async def recount_lagging_users(
    *, limit: int = 800, concurrency: int = 6
) -> dict[str, int]:
    """Fix used_traffic=0 / lag when panel_accounts already have meters."""
    ids = await find_lagging_usage_user_ids(limit=limit)
    if not ids:
        return {"users": 0, "flipped": 0}
    flipped = await recount_users_usage(ids, concurrency=max(2, int(concurrency)))
    logger.info(
        "lagging recount users=%s flipped=%s",
        len(ids),
        len(list(dict.fromkeys(flipped))),
    )
    return {
        "users": len(ids),
        "flipped": len(list(dict.fromkeys(flipped))),
    }


async def run_fleet_usage_cycle(*, rotate_only: bool = False) -> dict[str, int]:
    """Fleet usage: Astra + remote bulk (full or rotate) + lagging recount.

    ``rotate_only`` pulls a slice of panels each tick so meters stay fresh
    without a multi-minute full pass blocking the event loop.
    """
    global _FLEET_CYCLE_N
    from app.services.orchestrator import sync_all_astra_traffic

    t0 = time.monotonic()
    _FLEET_CYCLE_N += 1
    # Full panel sweep less often; rotate keeps accuracy between sweeps.
    full_panels = (not rotate_only) and (_FLEET_CYCLE_N % 8 == 1)
    full_recount = _FLEET_CYCLE_N % 16 == 1
    stats: dict[str, int] = {
        "astra_panels": 0,
        "panels": 0,
        "listed": 0,
        "dirty": 0,
        "flipped": 0,
        "recount_users": 0,
        "lagging": 0,
        "full_pass": int(full_panels),
        "rotate": int(not full_panels),
    }
    try:
        from app.core.database import pool_pressure_blocks_fleet

        if pool_pressure_blocks_fleet():
            # Still try lagging recount — DB-only, fixes used=0 when meters exist.
            try:
                lag = await recount_lagging_users(limit=400, concurrency=4)
                stats["lagging"] = int(lag.get("users") or 0)
                stats["flipped"] += int(lag.get("flipped") or 0)
            except Exception:  # noqa: BLE001
                pass
            stats["skipped_pool"] = 1
            stats["dt"] = int(time.monotonic() - t0)
            logger.warning(
                "fleet remote skipped (critical pool) — lagging=%s",
                stats["lagging"],
            )
            return stats
    except Exception:  # noqa: BLE001
        pass
    try:
        stats["astra_panels"] = int(await sync_all_astra_traffic() or 0)
    except Exception:  # noqa: BLE001
        logger.warning("fleet astra sync failed", exc_info=True)

    try:
        if full_panels:
            bulk = await sync_all_panels_bulk(
                concurrency=3, recount=True, recount_concurrency=4
            )
        else:
            # 4/tick: full PasarGuard list is heavy; more panels starved the DB pool
            # and froze hot priority pulls (used_traffic stuck while remote grew).
            bulk = await sync_panels_bulk_rotate(max_panels=4)
        stats["panels"] = int(bulk.get("panels") or 0)
        stats["listed"] = int(bulk.get("listed") or 0)
        stats["dirty"] = int(bulk.get("dirty") or 0)
        stats["flipped"] += int(bulk.get("flipped") or 0)
    except Exception:  # noqa: BLE001
        logger.warning("fleet remote bulk failed", exc_info=True)

    # Always close the user.used_traffic gap from meters already on disk.
    # Prefer dirty ids left by worker usage-report queue (cheap, accurate).
    try:
        from app.services.worker_ops import drain_report_dirty_for_recount

        report_dirty = await drain_report_dirty_for_recount(
            limit=600 if full_panels else 350
        )
        if report_dirty:
            flipped_rep = await recount_users_usage(
                report_dirty, concurrency=4
            )
            stats["flipped"] += len(list(dict.fromkeys(flipped_rep)))
            stats["report_dirty"] = len(report_dirty)
    except Exception:  # noqa: BLE001
        logger.warning("fleet report-dirty recount failed", exc_info=True)

    try:
        # Lighter when workers own most remote panels — avoid competing with
        # the usage-report queue on the single event-loop core.
        lag = await recount_lagging_users(
            limit=500 if full_panels else 280,
            concurrency=4,
        )
        stats["lagging"] = int(lag.get("users") or 0)
        stats["flipped"] += int(lag.get("flipped") or 0)
    except Exception:  # noqa: BLE001
        logger.warning("fleet lagging recount failed", exc_info=True)

    # Raw meters already past data_limit but used_traffic lagged → flip+ready freeze
    try:
        catch = await recount_and_flip_exhausted(limit=500, concurrency=5)
        stats["lagging"] = max(
            int(stats.get("lagging") or 0), int(catch.get("lagging") or 0)
        )
        stats["flipped"] += int(catch.get("flipped") or 0)
        stats["raw_over"] = int(catch.get("raw_over") or 0)
    except Exception:  # noqa: BLE001
        logger.warning("fleet recount/flip exhausted failed", exc_info=True)

    if full_recount:
        try:
            full = await recount_all_live_users(concurrency=4, chunk=400)
            stats["recount_users"] = int(full.get("users") or 0)
            stats["flipped"] += int(full.get("flipped") or 0)
        except Exception:  # noqa: BLE001
            logger.warning("fleet full recount failed", exc_info=True)

    stats["dt"] = int(time.monotonic() - t0)
    logger.info(
        "fleet usage cycle astra=%s panels=%s listed=%s dirty=%s "
        "lagging=%s recount=%s full=%s flipped=%s dt=%ss",
        stats["astra_panels"],
        stats["panels"],
        stats["listed"],
        stats["dirty"],
        stats["lagging"],
        stats["recount_users"],
        full_panels,
        stats["flipped"],
        stats["dt"],
    )
    return stats


async def find_exhausted_user_ids(*, limit: int = 200) -> list[int]:
    """SQL: active users whose stored used_traffic already hit data_limit."""
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT id FROM users "
                "WHERE status = 'active' AND data_limit > 0 "
                "AND used_traffic >= data_limit "
                "ORDER BY used_traffic DESC LIMIT :lim"
            ),
            {"lim": max(1, int(limit))},
        )
        return [int(x) for (x,) in result.all()]


async def find_unfrozen_blocked_user_ids(*, limit: int = 500) -> list[int]:
    """limited/expired users that still have live remotes needing KILL_USER.

    Matches accounts still ``active``/``error`` without the quota-frozen mark —
    these keep downloading after Astra already flipped status.
    """
    from sqlalchemy import text

    mark = str(QUOTA_FROZEN_MARK)
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                text(
                    """
                    SELECT DISTINCT u.id
                    FROM users u
                    JOIN panel_accounts pa ON pa.user_id = u.id
                    WHERE u.status IN ('limited', 'expired', 'disabled')
                      AND pa.status IN ('active', 'error')
                      AND (
                        pa.error_message IS NULL
                        OR pa.error_message = ''
                        OR pa.error_message <> :mark
                      )
                    ORDER BY u.id DESC
                    LIMIT :lim
                    """
                ),
                {"lim": max(1, int(limit)), "mark": mark},
            )
        ).all()
    return [int(r[0]) for r in rows]


async def heal_exhausted_and_unfrozen(*, limit: int = 400) -> dict[str, int]:
    """Flip active-over-quota users and KILL every blocked user with live remotes.

    Never KILL from a pre-recount candidate list — after weighted recount a
    user may no longer be limited (inflated ``users.used_traffic``). Only
    freeze users still ``limited``/``expired``/``disabled``.
    """
    from app.services.orchestrator import (
        enforce_user_quota,
        kick_freeze_user,
        recount_user_used_traffic,
    )

    exhausted = await find_exhausted_user_ids(limit=limit)
    flipped = 0
    still_blocked: list[int] = []
    for uid in exhausted:
        try:
            async with AsyncSessionLocal() as db:
                user, accounts = await _load_user_for_recount(db, int(uid))
                if user is None:
                    continue
                # Shrink allowed: candidates came from stored used>=limit which
                # may be inflated vs Σ panel meters.
                await recount_user_used_traffic(
                    db, user, accounts, allow_shrink=True
                )
                if await enforce_user_quota(db, user):
                    flipped += 1
                await db.commit()
                if user.status in (
                    UserStatus.limited,
                    UserStatus.expired,
                    UserStatus.disabled,
                ):
                    still_blocked.append(int(uid))
        except Exception:  # noqa: BLE001
            logger.warning("heal flip user_id=%s failed", uid, exc_info=True)

    unfrozen = await find_unfrozen_blocked_user_ids(limit=limit)
    kill_ids = list(dict.fromkeys([*still_blocked, *unfrozen]))
    from app.services.worker_commands import enqueue_kill_user

    for uid in kill_ids:
        try:
            # Respect kill cooldown — force=True was flooding Redis (500-cap
            # LTRIM dropped waiting fresh over-limit kills for minutes).
            from app.services.worker_commands import kill_on_cooldown

            if kill_on_cooldown(int(uid)):
                continue
            # Await enqueue so ASTRA_QUOTA_FROZEN is stamped before we return
            # (kick_freeze alone schedules a task and heal would re-select them).
            await enqueue_kill_user(int(uid), force=False)
            await kick_freeze_user(int(uid), remote_timeout=8.0, force=False)
        except Exception:  # noqa: BLE001
            logger.debug("heal kill user_id=%s failed", uid, exc_info=True)
        await asyncio.sleep(0.02)
    logger.info(
        "heal_exhausted flipped=%s exhausted=%s still_blocked=%s unfrozen=%s kill=%s",
        flipped,
        len(exhausted),
        len(still_blocked),
        len(unfrozen),
        len(kill_ids),
    )
    return {
        "flipped": flipped,
        "exhausted": len(exhausted),
        "unfrozen": len(unfrozen),
        "kill": len(kill_ids),
        "still_blocked": len(still_blocked),
    }


async def find_raw_over_limit_user_ids(*, limit: int = 400) -> list[int]:
    """Active users where Σ *live* panel *weighted* meters already exceed data_limit.

    Uses ``raw × applied_coefficient`` (not raw alone) so high-coeff panels do
    not false-trigger, and coeff&lt;1 panels are not ignored. Deleted meters are
    excluded. Lagging ``users.used_traffic`` after live meters pass the limit
    means remotes stay online until recount+enforce flips them.
    """
    from sqlalchemy import text

    lim = max(1, min(2000, int(limit)))
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                text(
                    """
                    SELECT u.id
                    FROM users u
                    JOIN panel_accounts pa ON pa.user_id = u.id
                      AND pa.status IN ('active', 'error', 'pending')
                    WHERE u.status = 'active'
                      AND u.data_limit > 0
                    GROUP BY u.id, u.data_limit, u.used_traffic
                    HAVING coalesce(sum(
                             (coalesce(pa.used_traffic, 0)::numeric
                              * coalesce(pa.applied_coefficient, 1.0))::bigint
                           ), 0) >= u.data_limit
                       AND coalesce(u.used_traffic, 0) < u.data_limit
                    ORDER BY (
                      coalesce(sum(
                        (coalesce(pa.used_traffic, 0)::numeric
                         * coalesce(pa.applied_coefficient, 1.0))::bigint
                      ), 0) - coalesce(u.used_traffic, 0)
                    ) DESC
                    LIMIT :lim
                    """
                ),
                {"lim": lim},
            )
        ).all()
    return [int(r[0]) for r in rows]


async def recount_and_flip_exhausted(
    *, limit: int = 500, concurrency: int = 6
) -> dict[str, int]:
    """Recount lagging / raw-over users, flip quota, return flip counts."""
    from app.services.orchestrator import (
        enforce_user_quota,
        recount_user_used_traffic,
    )

    lag_ids = await find_lagging_usage_user_ids(limit=limit)
    raw_ids = await find_raw_over_limit_user_ids(limit=max(100, limit // 2))
    ids = list(dict.fromkeys([*lag_ids, *raw_ids]))
    flipped = 0
    if ids:
        flipped = len(
            await recount_users_usage(ids, concurrency=max(2, int(concurrency)))
        )
    # Anyone still active with used >= limit after recount
    exhausted = await find_exhausted_user_ids(limit=limit)
    extra = 0
    confirmed: list[int] = []
    for uid in exhausted:
        try:
            async with AsyncSessionLocal() as db:
                user, accounts = await _load_user_for_recount(db, int(uid))
                if user is None:
                    continue
                await recount_user_used_traffic(
                    db, user, accounts, allow_shrink=True
                )
                if await enforce_user_quota(db, user):
                    extra += 1
                await db.commit()
                if user.status in (
                    UserStatus.limited,
                    UserStatus.expired,
                    UserStatus.disabled,
                ):
                    confirmed.append(int(uid))
        except Exception:  # noqa: BLE001
            logger.debug(
                "recount_and_flip user_id=%s failed", uid, exc_info=True
            )
    # Immediate multi-panel cut only for users still blocked after recount.
    try:
        from app.services.orchestrator import kick_freeze_user

        for uid in confirmed[:120]:
            try:
                await kick_freeze_user(int(uid), remote_timeout=6.0)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    logger.info(
        "recount_and_flip lag=%s raw_over=%s flipped=%s extra=%s "
        "exhausted=%s confirmed=%s",
        len(lag_ids),
        len(raw_ids),
        flipped,
        extra,
        len(exhausted),
        len(confirmed),
    )
    return {
        "lagging": len(lag_ids),
        "raw_over": len(raw_ids),
        "flipped": flipped + extra,
        "exhausted": len(exhausted),
        "confirmed": len(confirmed),
    }


async def find_time_expired_user_ids(*, limit: int = 300) -> list[int]:
    """Active users whose expire unix timestamp has already passed."""
    import time

    from sqlalchemy import text

    now = int(time.time())
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT id FROM users "
                "WHERE status = 'active' "
                "AND expire IS NOT NULL AND expire > 0 AND expire <= :now "
                "ORDER BY expire ASC LIMIT :lim"
            ),
            {"lim": max(1, int(limit)), "now": now},
        )
        return [int(x) for (x,) in result.all()]


async def flip_time_expired_users(*, limit: int = 300) -> list[int]:
    """Mark due active users as expired. Returns flipped user ids."""
    import time

    from sqlalchemy import update

    from app.models.user import User, UserStatus

    now = int(time.time())
    ids = await find_time_expired_user_ids(limit=limit)
    if not ids:
        return []
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(User)
            .where(
                User.id.in_(ids),
                User.status == UserStatus.active,
                User.expire.is_not(None),
                User.expire > 0,
                User.expire <= now,
            )
            .values(status=UserStatus.expired)
        )
        await db.commit()
    return ids


async def find_limited_needing_freeze_ids(*, limit: int = 200) -> list[int]:
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT u.id FROM users u "
                "WHERE u.status IN ('limited', 'expired') "
                "AND EXISTS ("
                "  SELECT 1 FROM panel_accounts pa "
                "  JOIN panels p ON p.id = pa.panel_id AND p.is_active IS TRUE "
                "  WHERE pa.user_id = u.id "
                "  AND pa.status IN ('active', 'error', 'pending') "
                "  AND COALESCE(pa.error_message, '') <> :mark"
                ") "
                "ORDER BY CASE WHEN u.status = 'expired' THEN 0 ELSE 1 END, "
                "u.id DESC LIMIT :lim"
            ),
            {"lim": max(1, int(limit)), "mark": QUOTA_FROZEN_MARK},
        )
        return [int(x) for (x,) in result.all()]


async def find_expired_needing_freeze_ids(*, limit: int = 200) -> list[int]:
    """Expired users that still have unfrozen remotes on active panels."""
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT u.id FROM users u "
                "WHERE u.status = 'expired' "
                "AND EXISTS ("
                "  SELECT 1 FROM panel_accounts pa "
                "  JOIN panels p ON p.id = pa.panel_id AND p.is_active IS TRUE "
                "  WHERE pa.user_id = u.id "
                "  AND pa.status IN ('active', 'error', 'pending') "
                "  AND COALESCE(pa.error_message, '') <> :mark"
                ") "
                "ORDER BY u.expire ASC NULLS LAST, u.id DESC LIMIT :lim"
            ),
            {"lim": max(1, int(limit)), "mark": QUOTA_FROZEN_MARK},
        )
        return [int(x) for (x,) in result.all()]


async def find_false_limited_user_ids(*, limit: int = 200) -> list[int]:
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT id FROM users "
                "WHERE status = 'limited' AND data_limit > 0 "
                "AND used_traffic < data_limit "
                "ORDER BY (data_limit - used_traffic) DESC LIMIT :lim"
            ),
            {"lim": max(1, int(limit))},
        )
        return [int(x) for (x,) in result.all()]


async def find_inflated_limited_user_ids(*, limit: int = 200) -> list[int]:
    """limited users whose stored used ≫ Σ weighted meters and still under limit.

    Kept for callers that only want false-limited reactivation candidates.
    """
    return await find_inflated_used_user_ids(
        limit=limit, statuses=("limited",), only_under_limit=True
    )


async def find_inflated_used_user_ids(
    *,
    limit: int = 500,
    statuses: tuple[str, ...] = ("active", "limited", "on_hold"),
    only_under_limit: bool = False,
    gap_bytes: int = 50 * 1024 * 1024,
) -> list[int]:
    """Users whose ``users.used_traffic`` exceeds Σ(panel raw × coeff) by ``gap``.

    Covers lifetime-parse inflation and double delta bumps — including cases
    where weighted is still above ``data_limit`` (shrink the number, keep
    limited) and near-zero meter victims (shrink → often reactivate).
    """
    from sqlalchemy import text

    lim = max(1, min(10000, int(limit)))
    gap = max(0, int(gap_bytes))
    st = tuple(str(s) for s in statuses if s) or ("active", "limited", "on_hold")
    st_sql = ", ".join(f"'{s}'" for s in st)
    # Include pending (provision lag meters) — same live set as _weighted_total.
    # Excluding pending caused ~580 false «inflated» flags while used already
    # matched truth; reconcile then no-op'd and the bug looked permanent.
    under = (
        "AND coalesce(sum("
        "(coalesce(pa.used_traffic, 0)::numeric "
        "* coalesce(pa.applied_coefficient, 1.0))"
        "), 0) < u.data_limit"
        if only_under_limit
        else ""
    )
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                text(
                    f"""
                    SELECT u.id
                    FROM users u
                    LEFT JOIN panel_accounts pa ON pa.user_id = u.id
                      AND pa.status IN ('active', 'error', 'pending')
                    WHERE u.status IN ({st_sql})
                    GROUP BY u.id, u.used_traffic, u.data_limit
                    HAVING u.used_traffic::numeric > coalesce(sum(
                             (coalesce(pa.used_traffic, 0)::numeric
                              * coalesce(pa.applied_coefficient, 1.0))
                           ), 0) + :gap
                    {under}
                    ORDER BY (
                      u.used_traffic::numeric - coalesce(sum(
                        (coalesce(pa.used_traffic, 0)::numeric
                         * coalesce(pa.applied_coefficient, 1.0))
                      ), 0)
                    ) DESC
                    LIMIT :lim
                    """
                ),
                {"lim": lim, "gap": gap},
            )
        ).all()
    return [int(r[0]) for r in rows]


def _accounts_synced_enough_for_shrink(
    accounts: list, *, max_age_sec: float = 86400.0
) -> bool:
    """True when live meters look trustworthy enough to shrink user.used_traffic.

    Accept either a recent ``last_sync_at`` (≤24h) or a consistent
    ``used_traffic ≈ last_remote_raw`` watermark (panel meter already grounded).
    """
    from datetime import datetime, timezone

    from app.models.user import PanelAccountStatus

    now = datetime.now(timezone.utc)
    live = [
        a
        for a in accounts
        if a.status
        in (
            PanelAccountStatus.active,
            PanelAccountStatus.error,
            PanelAccountStatus.pending,
        )
        and int(getattr(a, "used_traffic", 0) or 0) >= 0
        and (
            a.status != PanelAccountStatus.pending
            or int(getattr(a, "used_traffic", 0) or 0) > 0
        )
    ]
    if not live:
        return False
    fresh = 0
    for a in live:
        ts = getattr(a, "last_sync_at", None)
        if ts is not None:
            if getattr(ts, "tzinfo", None) is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if (now - ts).total_seconds() <= max_age_sec:
                fresh += 1
                continue
        used = int(getattr(a, "used_traffic", 0) or 0)
        raw = int(getattr(a, "last_remote_raw", 0) or 0)
        # Grounded meter: stored matches last remote sample (or both zero).
        if used == 0 and raw == 0:
            fresh += 1
            continue
        if raw > 0 and abs(used - raw) <= max(1024 * 1024, int(raw * 0.02)):
            fresh += 1
    # Require a majority of live rows synced / grounded.
    return fresh >= max(1, (len(live) + 1) // 2)


async def activate_pending_with_usage(*, limit: int = 2000) -> dict[str, int]:
    """Promote pending accounts that already hold real traffic to active.

    Provision lag left meters on ``pending`` rows (~654GB fleet-wide). Weighted
    usage now counts them, and this pass finishes migration so UI/status match.
    """
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                """
                UPDATE panel_accounts
                SET status = 'active',
                    error_message = CASE
                        WHEN error_message = :frozen THEN NULL
                        ELSE error_message
                    END
                WHERE id IN (
                    SELECT pa.id FROM panel_accounts pa
                    JOIN panels p ON p.id = pa.panel_id AND p.is_active IS TRUE
                    WHERE pa.status = 'pending'
                      AND COALESCE(pa.used_traffic, 0) > 0
                    ORDER BY pa.used_traffic DESC
                    LIMIT :lim
                )
                RETURNING id
                """
            ),
            {"lim": max(1, int(limit)), "frozen": QUOTA_FROZEN_MARK},
        )
        rows = result.fetchall()
        await db.commit()
    n = len(rows)
    if n:
        logger.info("activate_pending_with_usage promoted=%s", n)
    return {"promoted": n}


def is_nx_inflated_meter(accumulated: int, last_remote: int | None) -> bool:
    """True when accumulated raw looks like false watermark re-adds.

    Classic zero-blip bug: ``last_remote_raw`` stays on the current cycle while
    ``used_traffic`` is re-added on every 0→N blip (exact 2×/3×/… or extreme
    ratios like 100×+ that cannot be real panel resets).
    """
    last_i = int(last_remote or 0)
    acc_i = int(accumulated or 0)
    if last_i <= 0 or acc_i < last_i * 2:
        return False
    ratio = acc_i / float(last_i)
    # Extreme multiples are virtually always false re-adds (hundreds of
    # identical panel resets of the same watermark do not happen in practice).
    if ratio >= 8.0 and last_i >= 10 * 1024 * 1024:  # ≥10MB watermark
        return True
    if last_i < 50 * 1024 * 1024:  # near-integer path needs ≥50MB
        return False
    k = int(round(ratio))
    if k < 2:
        return False
    return abs(ratio - k) <= 0.10


async def clamp_nx_inflated_panel_accounts(
    *, limit: int = 20000
) -> dict[str, Any]:
    """Clamp panel_accounts hit by N× watermark re-add; return affected user ids.

    Sets ``used_traffic = last_remote_raw`` when the ratio is a near-integer ≥2.
    Fast SQL filter first; Python confirms the integer-multiple heuristic.
    """
    from sqlalchemy import text

    lim = max(1, min(100000, int(limit)))
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                text(
                    """
                    SELECT id, user_id, used_traffic, last_remote_raw
                    FROM panel_accounts
                    WHERE status IN ('active', 'error', 'pending')
                      AND last_remote_raw IS NOT NULL
                      AND last_remote_raw > :min_last
                      AND used_traffic > last_remote_raw * 2
                    ORDER BY used_traffic DESC
                    LIMIT :lim
                    """
                ),
                # 10MB floor so extreme-ratio victims with small watermarks
                # (e.g. 0.5GB last × 1500) are still selected for Python check.
                {"min_last": 10 * 1024 * 1024, "lim": lim},
            )
        ).mappings().all()

        clamped = 0
        user_ids: set[int] = set()
        for r in rows:
            acc = int(r["used_traffic"] or 0)
            last = int(r["last_remote_raw"] or 0)
            if not is_nx_inflated_meter(acc, last):
                continue
            await db.execute(
                text(
                    """
                    UPDATE panel_accounts
                    SET used_traffic = :last,
                        coeff_anchor_raw = 0,
                        coeff_anchor_weighted = 0
                    WHERE id = :id
                    """
                ),
                {"last": last, "id": int(r["id"])},
            )
            clamped += 1
            user_ids.add(int(r["user_id"]))
        await db.commit()
    if clamped:
        logger.warning(
            "clamp_nx_inflated accounts=%s users=%s", clamped, len(user_ids)
        )
    return {"clamped": clamped, "user_ids": sorted(user_ids)}


async def heal_nx_meter_inflation(*, limit: int = 20000) -> dict[str, int]:
    """Clamp N×-inflated panel meters, recount victims, unfreeze if under quota."""
    from app.services.orchestrator import (
        enforce_user_quota,
        recount_user_used_traffic,
    )
    from sqlalchemy import text

    summary = await clamp_nx_inflated_panel_accounts(limit=limit)
    user_ids = list(summary.get("user_ids") or [])
    shrunk = 0
    reactivated = 0
    for uid in user_ids:
        try:
            async with AsyncSessionLocal() as db:
                user, accounts = await _load_user_for_recount(db, int(uid))
                if user is None:
                    continue
                before = int(user.used_traffic or 0)
                # Clear sticky freeze so remotes can revive after recount.
                await db.execute(
                    text(
                        """
                        UPDATE panel_accounts
                        SET error_message = NULL,
                            status = CASE
                              WHEN status = 'error' THEN 'active'
                              ELSE status
                            END
                        WHERE user_id = :uid
                          AND error_message = :mark
                        """
                    ),
                    {"uid": int(uid), "mark": QUOTA_FROZEN_MARK},
                )
                after = await recount_user_used_traffic(
                    db, user, accounts, allow_shrink=True
                )
                if after < before:
                    shrunk += 1
                changed = await enforce_user_quota(db, user)
                if user.status == UserStatus.active:
                    for acc in accounts:
                        if (acc.error_message or "") == QUOTA_FROZEN_MARK:
                            acc.error_message = None
                    try:
                        from app.services.worker_commands import clear_kill_cooldown

                        clear_kill_cooldown(int(uid))
                    except Exception:  # noqa: BLE001
                        pass
                await db.commit()
                if changed and user.status == UserStatus.active:
                    reactivated += 1
                elif (
                    user.status == UserStatus.limited
                    and int(user.data_limit or 0) > 0
                    and int(user.used_traffic or 0) < int(user.data_limit or 0)
                ):
                    user.status = UserStatus.active
                    await db.commit()
                    reactivated += 1
        except Exception:  # noqa: BLE001
            logger.warning("heal_nx user_id=%s failed", uid, exc_info=True)
        await asyncio.sleep(0)
    out = {
        "clamped": int(summary.get("clamped") or 0),
        "users": len(user_ids),
        "shrunk": shrunk,
        "reactivated": reactivated,
    }
    if out["clamped"] or out["users"]:
        logger.warning(
            "heal_nx_meter_inflation clamped=%s users=%s shrunk=%s reactivated=%s",
            out["clamped"],
            out["users"],
            out["shrunk"],
            out["reactivated"],
        )
    return out


async def reconcile_inflated_used_traffic(
    *, limit: int = 500, gap_bytes: int = 50 * 1024 * 1024
) -> dict[str, int]:
    """Shrink inflated ``users.used_traffic`` to deduped Σ weighted meters.

    First heals panel-level N× watermark re-adds, then shrinks user totals when
    a majority of live meters look fresh (≤2h). Deduped recount removes
    multi-group remote double-counts.
    """
    from app.services.orchestrator import (
        enforce_user_quota,
        recount_user_used_traffic,
    )

    nx = {"clamped": 0, "users": 0, "shrunk": 0, "reactivated": 0}
    try:
        nx = await heal_nx_meter_inflation(limit=max(2000, int(limit) * 20))
    except Exception:  # noqa: BLE001
        logger.warning("heal_nx_meter_inflation failed", exc_info=True)

    ids = await find_inflated_used_user_ids(limit=limit, gap_bytes=gap_bytes)
    shrunk = int(nx.get("shrunk") or 0)
    reactivated = int(nx.get("reactivated") or 0)
    skipped_stale = 0
    for uid in ids:
        try:
            async with AsyncSessionLocal() as db:
                user, accounts = await _load_user_for_recount(db, int(uid))
                if user is None:
                    continue
                before = int(user.used_traffic or 0)
                # Authority = stored weighted formula (pending+anchors), not the
                # coarse SQL scan. Skip phantoms that find_inflated still tags.
                from app.services.orchestrator import _weighted_total_from_stored

                truth_quick = int(_weighted_total_from_stored(accounts))
                if before <= truth_quick + int(gap_bytes):
                    continue
                synced = _accounts_synced_enough_for_shrink(accounts)
                # ≥500MB sticky inflate: shrink even if sync stamps are old —
                # panel meters already in DB are the source of truth.
                if not synced and (before - truth_quick) < 500 * 1024 * 1024:
                    skipped_stale += 1
                    continue
                after = await recount_user_used_traffic(
                    db, user, accounts, allow_shrink=True
                )
                changed = await enforce_user_quota(db, user)
                if user.status == UserStatus.active:
                    for acc in accounts:
                        if (acc.error_message or "") == QUOTA_FROZEN_MARK:
                            acc.error_message = None
                    try:
                        from app.services.worker_commands import clear_kill_cooldown

                        clear_kill_cooldown(int(uid))
                    except Exception:  # noqa: BLE001
                        pass
                await db.commit()
                if after < before:
                    shrunk += 1
                if changed and user.status == UserStatus.active:
                    reactivated += 1
        except Exception:  # noqa: BLE001
            logger.warning(
                "reconcile inflated user_id=%s failed", uid, exc_info=True
            )
        await asyncio.sleep(0)
    logger.info(
        "reconcile_inflated nx_clamped=%s nx_users=%s candidates=%s "
        "shrunk=%s reactivated=%s skipped_stale=%s",
        int(nx.get("clamped") or 0),
        int(nx.get("users") or 0),
        len(ids),
        shrunk,
        reactivated,
        skipped_stale,
    )
    return {
        "nx_clamped": int(nx.get("clamped") or 0),
        "nx_users": int(nx.get("users") or 0),
        "candidates": len(ids),
        "shrunk": shrunk,
        "reactivated": reactivated,
        "skipped_stale": skipped_stale,
    }


async def find_active_needing_unfreeze_ids(*, limit: int = 80) -> list[int]:
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT DISTINCT u.id FROM users u "
                "JOIN panel_accounts pa ON pa.user_id = u.id "
                "JOIN panels p ON p.id = pa.panel_id AND p.is_active IS TRUE "
                "WHERE u.status = 'active' "
                "AND u.data_limit > 0 AND u.used_traffic < u.data_limit "
                "AND pa.status IN ('active', 'error', 'pending') "
                "AND COALESCE(pa.error_message, '') = :mark "
                "ORDER BY u.id DESC LIMIT :lim"
            ),
            {"lim": max(1, int(limit)), "mark": QUOTA_FROZEN_MARK},
        )
        return [int(x) for (x,) in result.all()]


async def find_near_limit_user_ids(*, limit: int = 80) -> list[int]:
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT id FROM users "
                "WHERE status IN ('active','on_hold') AND data_limit > 0 "
                "AND used_traffic >= (data_limit * 35) / 100 "
                "ORDER BY (used_traffic::float / NULLIF(data_limit,0)) DESC "
                "LIMIT :lim"
            ),
            {"lim": max(1, int(limit))},
        )
        return [int(x) for (x,) in result.all()]
