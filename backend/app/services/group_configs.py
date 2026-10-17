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
    """Upsert GroupConfigItem rows from sample user's active panel links."""
    result = await db.execute(
        select(GroupConfigItem).where(GroupConfigItem.group_id == group.id)
    )
    existing = {i.config_key: i for i in result.scalars().all()}
    seen_keys: set[str] = set()
    order = 0

    share_prefixes = ("vless://", "vmess://", "trojan://", "ss://", "shadowsocks://", "tuic://", "hysteria://", "hy2://")

    accounts = await _load_panel_accounts(db, user.id)
    for account in accounts:
        if account.status != PanelAccountStatus.active:
            continue
        for link in account.links or []:
            if not link or not isinstance(link, str):
                continue
            raw = link.strip()
            if not raw:
                continue
            lower = raw.lower()
            # Keep protocol share links; skip plain http(s) subscription URLs
            is_share = lower.startswith(share_prefixes)
            is_http_sub = lower.startswith(("http://", "https://"))
            if is_http_sub and not is_share:
                continue
            if not is_share and not is_http_sub:
                # some panels return bare links without scheme — keep them
                pass
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

    await db.flush()
    # reload with panels
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
            accounts_out.append(
                GroupConfigSampleAccountOut(
                    panel_id=a.panel_id,
                    panel_name=a.panel.name if a.panel else None,
                    panel_type=a.panel.type.value if a.panel else None,
                    status=a.status.value if hasattr(a.status, "value") else str(a.status),
                    links_count=len(a.links or []),
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
        sample_ready=bool(any(a.status == PanelAccountStatus.active for a in accounts)),
        accounts=accounts_out,
        items=items,
    )


async def _force_reset_sample_remotes(
    db: AsyncSession,
    user: User,
    panels: list,
) -> None:
    """Delete sample user on every panel, then wipe local PanelAccount rows."""
    existing = await _load_panel_accounts(db, user.id)
    by_panel = {a.panel_id: a for a in existing}

    async def _remote_delete(panel, account: PanelAccount | None) -> None:
        # Rebecca soft-deletes users; recreating the same name then fails with
        # "already exists" / "User not found". Sample refresh upserts instead.
        if getattr(panel, "type", None) == PanelType.rebecca:
            return
        try:
            adapter = get_adapter(panel)
            await asyncio.wait_for(
                adapter.delete_user(user.username, account.remote_id if account else None),
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

    # Force recreate on remotes so «تازه‌سازی» always rebuilds
    await _force_reset_sample_remotes(db, user, panels)
    # Give remotes a moment after delete (avoids "user exists" / empty first response)
    await asyncio.sleep(0.6)

    user = await _get_sample_user(db, group.id)
    if not user:
        raise ValueError("کاربر نمونه پیدا نشد")
    # Drop stale relationship cache before provision/sync
    await db.refresh(user)

    await orchestrator.provision_user_on_panels(db, user, panels)

    user = await _get_sample_user(db, group.id)
    if not user:
        raise ValueError("ساخت کاربر نمونه ناموفق بود")

    # If Rebecca burned the sample username via soft-delete, rotate and retry once
    # panels are (panel, config, group_id) triples from resolve_panels_for_groups
    rebecca_panels = [
        (p, c, g)
        for p, c, g in panels
        if getattr(p, "type", None) == PanelType.rebecca
    ]
    if rebecca_panels:
        accounts = await _load_panel_accounts(db, user.id)
        rebecca_ids = {p.id for p, _c, _g in rebecca_panels}
        trapped = [
            a
            for a in accounts
            if a.panel_id in rebecca_ids
            and a.status != PanelAccountStatus.active
            and (
                "soft-delete" in (a.error_message or "").lower()
                or "user not found" in (a.error_message or "").lower()
                or "already exists" in (a.error_message or "").lower()
            )
        ]
        if trapped:
            import secrets

            new_name = f"ag{group.id}s{secrets.token_hex(3)}"[:32]
            user.username = new_name
            await db.flush()
            # wipe local rebecca accounts then reprovision only those panels
            for a in accounts:
                if a.panel_id in rebecca_ids:
                    await db.delete(a)
            await db.flush()
            await orchestrator.provision_user_on_panels(db, user, rebecca_panels)
            user = await _get_sample_user(db, group.id)
            if not user:
                raise ValueError("ساخت کاربر نمونه ناموفق بود")

    # Pull links: create response is sometimes empty until a follow-up GET
    for attempt in range(3):
        accounts = await _load_panel_accounts(db, user.id)
        active = [a for a in accounts if a.status == PanelAccountStatus.active]
        with_links = [a for a in active if a.links]
        if active and (with_links or attempt == 2):
            break
        try:
            await orchestrator.refresh_usage(db, user)
        except Exception:  # noqa: BLE001
            pass
        if attempt < 2:
            await asyncio.sleep(0.5)

    # Fresh item list from all share links
    await db.execute(delete(GroupConfigItem).where(GroupConfigItem.group_id == group.id))
    await db.flush()

    items = await sync_items_from_sample(db, group, user)
    for item in items:
        key = (item.panel_id, item.protocol or "", item.label or "")
        if key in saved_coeff:
            item.coefficient = saved_coeff[key]

    await db.commit()
    # New transaction view — expire so workspace does not reuse stale collections
    db.expire_all()
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
