"""Group sample configs: probe user + per-link coefficient / IP limit rules."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from urllib.parse import unquote

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.adapters.factory import get_adapter
from app.adapters.http_utils import is_share_link, is_subscription_ref
from app.models.group import Group, GroupPanel, UserGroup
from app.models.panel import PanelType
from app.models.group_config import GroupConfigItem
from app.models.user import PanelAccount, PanelAccountStatus, User, UserStatus
from app.schemas.group_config import (
    GroupConfigBulkUpdate,
    GroupConfigItemOut,
    GroupConfigSampleAccountOut,
    GroupConfigWorkspaceOut,
)
from app.services import orchestrator


def sample_username_for_group(group_id: int) -> str:
    # Marzban username: 3–32 chars, a-z / 0-9 / underscore only
    name = f"astra_g{group_id}_sample"
    return name[:32].lower()


def _share_links_of(account: PanelAccount) -> list[str]:
    """Real share configs only — http(s)/sub refs do not count."""
    out: list[str] = []
    for raw in account.links or []:
        if not raw or not isinstance(raw, str):
            continue
        text = raw.strip()
        if text and is_share_link(text):
            out.append(text)
    return out


def _account_is_active(account: PanelAccount) -> bool:
    val = getattr(account.status, "value", account.status)
    return val == PanelAccountStatus.active.value or val == "active"


async def _hydrate_sample_share_links(db: AsyncSession, user: User) -> int:
    """Expand /sub refs into vless/vmess/… share links on the sample user."""
    # Ensure relationship is loaded for hydrate_share_links
    reloaded = await db.execute(
        select(User)
        .where(User.id == user.id)
        .options(selectinload(User.panel_accounts).selectinload(PanelAccount.panel))
        .execution_options(populate_existing=True)
    )
    fresh = reloaded.scalar_one_or_none() or user
    try:
        return int(
            await asyncio.wait_for(
                orchestrator.hydrate_share_links(db, fresh),
                timeout=18.0,
            )
        )
    except Exception:  # noqa: BLE001
        return 0


def _parse_link_meta(link: str) -> tuple[str, str, str]:
    """
    Return (protocol, label, fingerprint_host).
    fingerprint_host helps keep keys stable across uuid rotation.
    """
    raw = (link or "").strip()
    if not raw:
        return "unknown", "config", ""

    lower = raw.lower()
    protocol = "unknown"
    label = ""
    host_part = ""

    if lower.startswith("vmess://"):
        protocol = "vmess"
        try:
            b64 = raw.split("://", 1)[1]
            pad = "=" * (-len(b64) % 4)
            data = json.loads(base64.urlsafe_b64decode(b64 + pad).decode("utf-8", errors="ignore"))
            label = str(data.get("ps") or data.get("remark") or "")
            host_part = f"{data.get('add') or ''}:{data.get('port') or ''}"
            if data.get("net"):
                host_part += f"|{data.get('net')}"
        except Exception:
            label = "vmess"
    elif "://" in raw:
        scheme, rest = raw.split("://", 1)
        protocol = scheme.lower()
        # remark after #
        if "#" in rest:
            rest, frag = rest.split("#", 1)
            label = unquote(frag)
        # host after @
        if "@" in rest:
            host_part = rest.split("@", 1)[1].split("?", 1)[0].split("/", 1)[0]
        else:
            host_part = rest.split("?", 1)[0].split("/", 1)[0][:120]
    else:
        label = raw[:40]

    if not label:
        label = f"{protocol} · {host_part}" if host_part else protocol

    # sanitize fingerprint
    host_part = re.sub(r"[^\w.\-:|]", "", host_part)[:120]
    return protocol, label[:200], host_part


def make_config_key(panel_id: int, link: str) -> str:
    """Unique key per share link so every config is listed separately."""
    protocol, _label, host = _parse_link_meta(link)
    digest = hashlib.sha1((link or "").encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"p{panel_id}:{protocol}:{host or 'x'}:{digest}"[:255]


def _item_out(item: GroupConfigItem) -> GroupConfigItemOut:
    panel = item.panel
    return GroupConfigItemOut(
        id=item.id,
        group_id=item.group_id,
        panel_id=item.panel_id,
        panel_name=panel.name if panel else None,
        panel_type=panel.type.value if panel else None,
        config_key=item.config_key,
        label=item.label,
        protocol=item.protocol,
        sample_link=item.sample_link,
        coefficient=float(item.coefficient or 1),
        ip_limit_enabled=bool(item.ip_limit_enabled),
        ip_limit=int(item.ip_limit or 0),
        sort_order=int(item.sort_order or 0),
        updated_at=item.updated_at,
        created_at=item.created_at,
    )


async def _load_group(db: AsyncSession, group_id: int) -> Group | None:
    result = await db.execute(
        select(Group)
        .where(Group.id == group_id)
        .options(selectinload(Group.panel_links).selectinload(GroupPanel.panel))
    )
    return result.scalar_one_or_none()


async def _get_sample_user(db: AsyncSession, group_id: int) -> User | None:
    uname = sample_username_for_group(group_id)
    result = await db.execute(
        select(User)
        .where(User.username == uname)
        .options(selectinload(User.panel_accounts).selectinload(PanelAccount.panel))
        .execution_options(populate_existing=True)
    )
    user = result.scalar_one_or_none()
    if user:
        return user
    # Rotated Rebecca sample names: ag{id}sxxxxxx
    result2 = await db.execute(
        select(User)
        .join(UserGroup, UserGroup.user_id == User.id)
        .where(
            UserGroup.group_id == group_id,
            or_(
                User.username.like(f"ag{group_id}s%"),
                User.note.like("%نمونه کانفیگ%"),
            ),
        )
        .options(selectinload(User.panel_accounts).selectinload(PanelAccount.panel))
        .execution_options(populate_existing=True)
        .order_by(User.id.desc())
        .limit(1)
    )
    return result2.scalar_one_or_none()


async def _load_panel_accounts(db: AsyncSession, user_id: int) -> list[PanelAccount]:
    """Always query accounts fresh — relationship cache is stale with expire_on_commit=False."""
    result = await db.execute(
        select(PanelAccount)
        .where(PanelAccount.user_id == user_id)
        .options(selectinload(PanelAccount.panel))
        .execution_options(populate_existing=True)
        .order_by(PanelAccount.panel_id)
    )
    return list(result.scalars().all())


async def _ensure_sample_user(db: AsyncSession, group: Group) -> User:
    uname = sample_username_for_group(group.id)
    user = await _get_sample_user(db, group.id)
    if user is None:
        user = User(
            username=uname,
            status=UserStatus.active,
            data_limit=0,
            expire=0,
            note=f"نمونه کانفیگ گروه {group.name} — حذف دستی نکنید",
            proxy_settings={},
        )
        db.add(user)
        await db.flush()
        db.add(UserGroup(user_id=user.id, group_id=group.id))
        await db.flush()
    else:
        # ensure membership
        existing = await db.execute(
            select(UserGroup).where(UserGroup.user_id == user.id, UserGroup.group_id == group.id)
        )
        if existing.scalar_one_or_none() is None:
            db.add(UserGroup(user_id=user.id, group_id=group.id))
            await db.flush()
    # reload with accounts
    reloaded = await _get_sample_user(db, group.id)
    return reloaded or user


async def sync_items_from_sample(db: AsyncSession, group: Group, user: User) -> list[GroupConfigItem]:
    """Upsert GroupConfigItem rows from sample user's active share links only."""
    result = await db.execute(
        select(GroupConfigItem).where(GroupConfigItem.group_id == group.id)
    )
    existing = {i.config_key: i for i in result.scalars().all()}
    seen_keys: set[str] = set()
    order = 0

    accounts = await _load_panel_accounts(db, user.id)
    for account in accounts:
        if not _account_is_active(account):
            continue
        for raw in _share_links_of(account):
            key = make_config_key(account.panel_id, raw)
            seen_keys.add(key)
            protocol, label, _host = _parse_link_meta(raw)
            item = existing.get(key)
            if item is None:
                item = GroupConfigItem(
                    group_id=group.id,
                    panel_id=account.panel_id,
                    config_key=key,
                    label=label,
                    protocol=protocol,
                    sample_link=raw,
                    coefficient=1.0,
                    ip_limit_enabled=False,
                    ip_limit=0,
                    sort_order=order,
                )
                db.add(item)
                existing[key] = item
            else:
                item.sample_link = raw
                item.protocol = protocol or item.protocol
                if not item.label:
                    item.label = label
                item.sort_order = order
            order += 1

    # Drop rows for panels no longer in this group, or links sample no longer has.
    # If no share links were found, keep previous rows (avoid empty wipe on hydrate lag).
    gp_rows = await db.execute(
        select(GroupPanel.panel_id).where(GroupPanel.group_id == group.id)
    )
    linked_panel_ids = {int(pid) for (pid,) in gp_rows.all() if pid is not None}
    for key, item in list(existing.items()):
        gone_panel = int(item.panel_id or 0) not in linked_panel_ids
        gone_link = bool(seen_keys) and key not in seen_keys
        if gone_panel or gone_link:
            await db.delete(item)

    await db.flush()
    result2 = await db.execute(
        select(GroupConfigItem)
        .where(GroupConfigItem.group_id == group.id)
        .options(selectinload(GroupConfigItem.panel))
        .order_by(GroupConfigItem.sort_order, GroupConfigItem.id)
    )
    return list(result2.scalars().all())


async def apply_panel_ip_limits(db: AsyncSession, group: Group, items: list[GroupConfigItem]) -> None:
    """
    Persist IP limit onto GroupPanel.config (for future users)
    and push to sample user on each remote panel.
    """
    # Aggregate per panel: if any item enables IP limit, use max limit among enabled
    per_panel: dict[int, int] = {}
    for item in items:
        if item.ip_limit_enabled and item.ip_limit > 0:
            prev = per_panel.get(item.panel_id, 0)
            per_panel[item.panel_id] = max(prev, int(item.ip_limit))

    for link in group.panel_links or []:
        cfg = dict(link.config or {})
        if link.panel_id in per_panel:
            cfg["limit_ip"] = per_panel[link.panel_id]
        else:
            cfg.pop("limit_ip", None)
        link.config = cfg

    await db.flush()

    sample = await _get_sample_user(db, group.id)
    if not sample:
        return

    # Push limit_ip via modify for each account (merge group protocols/inbounds)
    result = await db.execute(
        select(PanelAccount)
        .where(PanelAccount.user_id == sample.id)
        .options(selectinload(PanelAccount.panel))
    )
    accounts = result.scalars().all()

    import asyncio
    from datetime import datetime, timezone

    from app.adapters.base import AdapterResult, UserPayload
    from app.adapters.factory import get_adapter

    jobs: list[tuple[PanelAccount, UserPayload]] = []
    for account in accounts:
        panel = account.panel
        if not panel or not panel.is_active:
            continue
        limit_ip = per_panel.get(panel.id, 0)
        panel_cfg: dict = {}
        for link in group.panel_links or []:
            if link.panel_id == account.panel_id:
                panel_cfg = dict(link.config or {})
                break
        cfg = {
            **panel_cfg,
            "limit_ip": limit_ip,
            "proxy_settings": sample.proxy_settings or {},
            "data_limit_reset_strategy": sample.data_limit_reset_strategy,
        }
        explicit = (sample.proxy_settings or {}).get("proxies")
        if isinstance(explicit, dict) and explicit:
            cfg["proxies"] = explicit
        jobs.append(
            (
                account,
                UserPayload(
                    username=sample.username,
                    data_limit=sample.data_limit or 0,
                    expire=sample.expire or None,
                    status=sample.status.value,
                    note=sample.note,
                    uuid=account.uuid,
                    config=cfg,
                ),
            )
        )

    async def _remote(account: PanelAccount, payload: UserPayload) -> AdapterResult:
        panel = account.panel
        assert panel is not None
        adapter = get_adapter(panel)
        try:
            return await asyncio.wait_for(adapter.modify_user(payload), timeout=30)
        except Exception as exc:  # noqa: BLE001
            return AdapterResult(success=False, error=str(exc))

    if jobs:
        results = await asyncio.gather(*[_remote(a, p) for a, p in jobs])
        now = datetime.now(timezone.utc)
        for (account, _payload), res in zip(jobs, results):
            if res.success:
                account.status = PanelAccountStatus.active
                if res.links:
                    account.links = res.links
                account.error_message = None
                account.last_sync_at = now
            else:
                account.error_message = res.error
        await db.flush()


async def build_workspace(db: AsyncSession, group: Group) -> GroupConfigWorkspaceOut:
    sample = await _get_sample_user(db, group.id)
    accounts_out: list[GroupConfigSampleAccountOut] = []
    accounts: list[PanelAccount] = []
    if sample:
        accounts = await _load_panel_accounts(db, sample.id)
        for a in accounts:
            share_n = len(_share_links_of(a))
            accounts_out.append(
                GroupConfigSampleAccountOut(
                    panel_id=a.panel_id,
                    panel_name=a.panel.name if a.panel else None,
                    panel_type=a.panel.type.value if a.panel else None,
                    status=a.status.value if hasattr(a.status, "value") else str(a.status),
                    links_count=share_n,
                    error_message=a.error_message,
                )
            )

    result = await db.execute(
        select(GroupConfigItem)
        .where(GroupConfigItem.group_id == group.id)
        .options(selectinload(GroupConfigItem.panel))
        .order_by(GroupConfigItem.sort_order, GroupConfigItem.id)
    )
    items = [_item_out(i) for i in result.scalars().all()]
    return GroupConfigWorkspaceOut(
        group_id=group.id,
        group_name=group.name,
        sample_username=sample.username if sample else None,
        sample_ready=bool(
            any(_account_is_active(a) and _share_links_of(a) for a in accounts)
            or items
        ),
        accounts=accounts_out,
        items=items,
    )


async def sample_status_for_group(
    db: AsyncSession, group_id: int
) -> tuple[bool, str | None]:
    """Return (alive_on_panels, username) for the group's probe user."""
    sample = await _get_sample_user(db, group_id)
    if not sample:
        return False, None
    accounts = await _load_panel_accounts(db, sample.id)
    alive = any(a.status == PanelAccountStatus.active for a in accounts)
    return alive, sample.username


async def _force_reset_sample_remotes(
    db: AsyncSession,
    user: User,
    panels: list,
    *,
    include_rebecca: bool = False,
) -> None:
    """Delete sample user on remotes, then wipe local PanelAccount rows.

    Rebecca soft-deletes; on refresh we skip remote delete and upsert instead.
    Explicit cleanup (حذف نمونه) passes include_rebecca=True.
    """
    existing = await _load_panel_accounts(db, user.id)
    by_panel = {a.panel_id: a for a in existing}

    async def _remote_delete(panel, account: PanelAccount | None) -> None:
        if not include_rebecca and getattr(panel, "type", None) == PanelType.rebecca:
            return
        remote_name = (account.remote_username if account else None) or user.username
        try:
            adapter = get_adapter(panel)
            await asyncio.wait_for(
                adapter.delete_user(
                    remote_name,
                    account.remote_id if account else None,
                ),
                timeout=25,
            )
        except Exception:  # noqa: BLE001
            pass

    await asyncio.gather(
        *[
            _remote_delete(panel, by_panel.get(panel.id))
            for panel, *_rest in panels
        ]
    )

    for account in existing:
        await db.delete(account)
    await db.flush()


async def create_or_refresh_sample(db: AsyncSession, group_id: int) -> GroupConfigWorkspaceOut:
    """
    Always rebuild sample user on ALL group panels (delete + create),
    then list every share link as its own coefficient row.
    """
    group = await _load_group(db, group_id)
    if not group:
        raise ValueError("Group not found")
    if group.is_disabled:
        raise ValueError("گروه غیرفعال است")

    panels = await orchestrator.resolve_panels_for_groups(db, [group.id])
    if not panels:
        raise ValueError("این گروه هیچ پنل فعالی ندارد")

    user = await _ensure_sample_user(db, group)

    # Preserve coefficients across rebuild (match by panel + protocol + label)
    prev_items = (
        await db.execute(select(GroupConfigItem).where(GroupConfigItem.group_id == group.id))
    ).scalars().all()
    saved_coeff: dict[tuple[int, str, str], float] = {}
    for item in prev_items:
        saved_coeff[(item.panel_id, item.protocol or "", item.label or "")] = float(
            item.coefficient or 1
        )

    # Force recreate on remotes (incl. Rebecca) so «تازه‌سازی» always rebuilds
    await _force_reset_sample_remotes(db, user, panels, include_rebecca=True)
    await asyncio.sleep(0.6)

    user = await _get_sample_user(db, group.id)
    if not user:
        raise ValueError("کاربر نمونه پیدا نشد")

    # Rebecca soft-deletes usernames — seed a fresh remote name before create
    import secrets

    rebecca_panels = [
        (p, c, g)
        for p, c, g in panels
        if getattr(p, "type", None) == PanelType.rebecca
    ]
    if rebecca_panels:
        for panel, _cfg, gid in rebecca_panels:
            db.add(
                PanelAccount(
                    user_id=user.id,
                    panel_id=panel.id,
                    group_id=int(gid) if gid else None,
                    status=PanelAccountStatus.pending,
                    remote_username=f"ag{group.id}s{secrets.token_hex(3)}"[:32],
                )
            )
        await db.flush()

    await orchestrator.provision_user_on_panels(db, user, panels)

    user = await _get_sample_user(db, group.id)
    if not user:
        raise ValueError("ساخت کاربر نمونه ناموفق بود")

    # Rebecca still trapped → rotate remote name once and reprovision those panels
    if rebecca_panels:
        accounts = await _load_panel_accounts(db, user.id)
        by_panel = {int(a.panel_id): a for a in accounts}
        trapped_targets: list = []
        for panel, cfg, gid in rebecca_panels:
            acc = by_panel.get(int(panel.id))
            err = (acc.error_message or "").lower() if acc else ""
            bad = (
                acc is None
                or not _account_is_active(acc)
                or "soft-delete" in err
                or "already exists" in err
            )
            if not bad:
                continue
            if acc is not None:
                acc.remote_username = f"ag{group.id}s{secrets.token_hex(3)}"[:32]
                acc.status = PanelAccountStatus.pending
                acc.error_message = None
            else:
                db.add(
                    PanelAccount(
                        user_id=user.id,
                        panel_id=panel.id,
                        group_id=int(gid) if gid else None,
                        status=PanelAccountStatus.pending,
                        remote_username=f"ag{group.id}s{secrets.token_hex(3)}"[:32],
                    )
                )
            trapped_targets.append((panel, cfg, gid))
        if trapped_targets:
            await db.flush()
            await orchestrator.provision_user_on_panels(db, user, trapped_targets)
            user = await _get_sample_user(db, group.id)
            if not user:
                raise ValueError("ساخت کاربر نمونه ناموفق بود")

    # Expand /sub → real share links. Do NOT treat subscription URLs as success.
    share_total = 0
    for attempt in range(4):
        await _hydrate_sample_share_links(db, user)
        user = await _get_sample_user(db, group.id) or user
        accounts = await _load_panel_accounts(db, user.id)
        active = [a for a in accounts if _account_is_active(a)]
        share_total = sum(len(_share_links_of(a)) for a in active)
        still_need = any(
            (not _share_links_of(a))
            and (
                not (a.links or [])
                or any(is_subscription_ref(str(x)) for x in (a.links or []) if x)
            )
            for a in active
        )
        if active and share_total > 0 and not still_need:
            break
        if attempt < 3:
            await asyncio.sleep(0.45 + 0.25 * attempt)

    # Upsert items from share links — never wipe-all first (empty hydrate must not erase coeffs)
    items = await sync_items_from_sample(db, group, user)
    for item in items:
        key = (item.panel_id, item.protocol or "", item.label or "")
        if key in saved_coeff:
            item.coefficient = saved_coeff[key]

    await db.commit()
    # Do NOT expire_all() — parent loops (sample_all) still hold Group instances;
    # lazy refresh after expire causes MissingGreenlet under AsyncSession.
    return await get_workspace(db, group_id)


async def create_or_refresh_sample_all(db: AsyncSession) -> dict:
    """
    Sequentially rebuild probe users for every active sales group.
    One group at a time to avoid hammering remotes; hide rules stay intact.
    """
    from app.services import config_hides as hide_svc

    result = await db.execute(
        select(Group)
        .where(Group.is_disabled.is_(False))
        .order_by(Group.name, Group.id)
    )
    # Snapshot ids/names before any commit — objects may expire mid-loop
    group_meta = [
        (int(g.id), str(g.name or ""))
        for g in result.scalars().all()
        if g.id is not None
    ]
    rows: list[dict] = []
    ok_count = 0
    fail_count = 0

    for idx, (gid, gname) in enumerate(group_meta):
        try:
            ws = await create_or_refresh_sample(db, gid)
            ok_count += 1
            rows.append(
                {
                    "group_id": gid,
                    "group_name": gname,
                    "ok": True,
                    "sample_username": ws.sample_username,
                    "items_count": len(ws.items or []),
                    "error": None,
                }
            )
        except Exception as exc:  # noqa: BLE001
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
            fail_count += 1
            rows.append(
                {
                    "group_id": gid,
                    "group_name": gname,
                    "ok": False,
                    "sample_username": None,
                    "items_count": 0,
                    "error": str(exc)[:400],
                }
            )
        if idx + 1 < len(group_meta):
            await asyncio.sleep(0.35)

    visibility = await hide_svc.build_visibility_all(db)
    return {
        "ok_count": ok_count,
        "fail_count": fail_count,
        "groups": rows,
        "visibility": visibility,
    }


async def delete_sample_all(db: AsyncSession) -> dict:
    """Remove probe users from all active groups; keep hides + discovered items."""
    from app.services import config_hides as hide_svc

    result = await db.execute(
        select(Group)
        .where(Group.is_disabled.is_(False))
        .order_by(Group.name, Group.id)
    )
    group_meta = [
        (int(g.id), str(g.name or ""))
        for g in result.scalars().all()
        if g.id is not None
    ]
    rows: list[dict] = []
    ok_count = 0
    fail_count = 0

    for idx, (gid, gname) in enumerate(group_meta):
        try:
            ws = await delete_sample(db, gid)
            ok_count += 1
            rows.append(
                {
                    "group_id": gid,
                    "group_name": gname,
                    "ok": True,
                    "sample_username": ws.sample_username,
                    "items_count": len(ws.items or []),
                    "error": None,
                }
            )
        except Exception as exc:  # noqa: BLE001
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
            fail_count += 1
            rows.append(
                {
                    "group_id": gid,
                    "group_name": gname,
                    "ok": False,
                    "sample_username": None,
                    "items_count": 0,
                    "error": str(exc)[:400],
                }
            )
        if idx + 1 < len(group_meta):
            await asyncio.sleep(0.25)

    visibility = await hide_svc.build_visibility_all(db)
    return {
        "ok_count": ok_count,
        "fail_count": fail_count,
        "groups": rows,
        "visibility": visibility,
    }


async def delete_sample(db: AsyncSession, group_id: int) -> GroupConfigWorkspaceOut:
    """
    Remove the probe/test user from every panel of the group (+ local User).

    Keeps GroupConfigItem rows (discovered endpoints) and all hide rules
    (host/port/match_host) so visibility settings survive. Next refresh
    creates a fresh sample again.
    """
    group = await _load_group(db, group_id)
    if not group:
        raise ValueError("Group not found")

    user = await _get_sample_user(db, group.id)
    if user is None:
        return await get_workspace(db, group_id)

    panels = await orchestrator.resolve_panels_for_groups(db, [group.id])
    if panels:
        await _force_reset_sample_remotes(
            db, user, panels, include_rebecca=True
        )

    # Drop local probe user (accounts already wiped above; cascade safety)
    await db.execute(delete(UserGroup).where(UserGroup.user_id == user.id))
    remaining = await _load_panel_accounts(db, user.id)
    for account in remaining:
        await db.delete(account)
    await db.delete(user)

    await db.commit()
    # Avoid expire_all() — shared AsyncSession + parent loops (see sample_all).
    return await get_workspace(db, group_id)


async def get_workspace(db: AsyncSession, group_id: int) -> GroupConfigWorkspaceOut:
    group = await _load_group(db, group_id)
    if not group:
        raise ValueError("Group not found")
    return await build_workspace(db, group)


async def update_workspace_items(
    db: AsyncSession, group_id: int, body: GroupConfigBulkUpdate
) -> GroupConfigWorkspaceOut:
    group = await _load_group(db, group_id)
    if not group:
        raise ValueError("Group not found")

    result = await db.execute(
        select(GroupConfigItem)
        .where(GroupConfigItem.group_id == group_id)
        .options(selectinload(GroupConfigItem.panel))
    )
    by_id = {i.id: i for i in result.scalars().all()}
    for upd in body.items:
        item = by_id.get(upd.id)
        if not item:
            continue
        item.coefficient = float(upd.coefficient)
        item.ip_limit_enabled = bool(upd.ip_limit_enabled)
        item.ip_limit = int(upd.ip_limit or 0)
        if upd.label is not None and upd.label.strip():
            item.label = upd.label.strip()[:200]

    # IP limit is configured on the Groups page (GroupPanel.config), not here.
    await db.commit()
    group = await _load_group(db, group_id)
    return await build_workspace(db, group)  # type: ignore[arg-type]
