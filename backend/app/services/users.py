from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select, text

from app.models.user import PanelAccountStatus, User
from app.schemas.user import PanelAccountOut, UserResponse
from app.services.site_settings import get_sub_base_url


def subscription_url(user: User) -> str:
    base = get_sub_base_url().rstrip("/")
    return f"{base}/sub/{user.sub_token}"


async def panel_account_share_meta(
    db: AsyncSession, account_ids: list[int]
) -> dict[int, tuple[bool, int]]:
    """Batch: account_id → (has real share link, link array length).

    List endpoints defer ``links`` JSON — this cheap text probe keeps UI
    «N/M پنل» honest without shipping full vless payloads.
    """
    ids = [int(x) for x in account_ids if x]
    if not ids:
        return {}
    out: dict[int, tuple[bool, int]] = {}
    # Chunk to stay under param limits on large pages.
    for i in range(0, len(ids), 800):
        chunk = ids[i : i + 800]
        rows = await db.execute(
            text(
                """
                SELECT pa.id,
                  (
                    pa.links IS NOT NULL
                    AND trim(coalesce(pa.links::text, '')) NOT IN ('', '[]', 'null', 'Null')
                    AND (
                      pa.links::text ILIKE '%vless://%'
                      OR pa.links::text ILIKE '%vmess://%'
                      OR pa.links::text ILIKE '%trojan://%'
                      OR pa.links::text ILIKE '%ss://%'
                      OR pa.links::text ILIKE '%shadowsocks://%'
                      OR pa.links::text ILIKE '%hysteria%'
                      OR pa.links::text ILIKE '%hy2://%'
                      OR pa.links::text ILIKE '%tuic://%'
                      OR pa.links::text ILIKE '%wireguard://%'
                    )
                  ) AS has_share,
                  CASE
                    WHEN pa.links IS NULL THEN 0
                    WHEN json_typeof(pa.links::json) = 'array'
                      THEN json_array_length(pa.links::json)
                    ELSE 0
                  END AS cfg_n
                FROM panel_accounts pa
                WHERE pa.id = ANY(:ids)
                """
            ),
            {"ids": chunk},
        )
        for r in rows.all():
            out[int(r[0])] = (bool(r[1]), int(r[2] or 0))
    return out


async def load_user(db: AsyncSession, username: str) -> User | None:
    from app.models.group import Group, GroupPanel
    from app.models.user import PanelAccount

    result = await db.execute(
        select(User)
        .where(User.username == username)
        .options(
            selectinload(User.groups).selectinload(Group.panel_links).selectinload(
                GroupPanel.panel
            ),
            selectinload(User.panel_accounts).selectinload(PanelAccount.panel),
        )
    )
    return result.scalar_one_or_none()


async def load_user_by_token_light(db: AsyncSession, token: str) -> User | None:
    """User row only — for cache-hit /sub Update (no groups/accounts join)."""
    result = await db.execute(select(User).where(User.sub_token == token))
    return result.scalar_one_or_none()


async def load_user_by_token_accounts(db: AsyncSession, token: str) -> User | None:
    """User + panel_accounts(+panel) only — emergency/fast /sub (no groups tree)."""
    from app.models.user import PanelAccount

    result = await db.execute(
        select(User)
        .where(User.sub_token == token)
        .options(
            selectinload(User.panel_accounts).selectinload(PanelAccount.panel),
        )
    )
    return result.scalar_one_or_none()


async def load_user_by_token(db: AsyncSession, token: str) -> User | None:
    from app.models.group import Group, GroupPanel
    from app.models.user import PanelAccount

    result = await db.execute(
        select(User)
        .where(User.sub_token == token)
        .options(
            selectinload(User.groups).selectinload(Group.panel_links).selectinload(
                GroupPanel.panel
            ),
            selectinload(User.panel_accounts).selectinload(PanelAccount.panel),
        )
    )
    return result.scalar_one_or_none()


def to_user_response(
    user: User,
    *,
    include_links: bool = True,
    share_meta: dict[int, tuple[bool, int]] | None = None,
) -> UserResponse:
    """Serialize user for API.

    include_links=False for list endpoints — cuts payload size a lot when
    many users each have large share-link arrays. List queries may ``defer``
    ``links``; never touch deferred attrs (async lazy-load is unsafe).

    ``share_meta`` maps account_id → (has_share_link, configs_count) from a
    batch SQL probe so list cards still show accurate «N/M پنل».
    """
    from app.adapters.http_utils import is_share_link

    links: list[str] = []
    seen: set[str] = set()
    accounts = list(user.__dict__.get("panel_accounts") or [])
    if include_links:
        for account in accounts:
            for link in (account.__dict__.get("links") or []):
                if link and link not in seen:
                    seen.add(link)
                    links.append(link)

    panel_accounts = []
    for a in accounts:
        # Only __dict__ / already-loaded attrs — never ORM getattr (MissingGreenlet).
        adict = a.__dict__
        panel = adict.get("panel")
        pdict = panel.__dict__ if panel is not None else {}
        used = int(adict.get("used_traffic") or 0)
        status_val = adict.get("status")
        is_deleted = status_val == PanelAccountStatus.deleted
        panel_active = bool(pdict.get("is_active", True)) if panel is not None else False

        # Deleted remotes keep banked ``used_traffic`` for quota. The Users
        # «مصرف پنل‌ها» modal must still list them — hiding them made total
        # traffic look orphaned after remove-panel / remove-group.
        if is_deleted:
            if used <= 0:
                continue
        elif not panel_active:
            # Panel off/missing: still show rows that contribute to quota.
            if used <= 0:
                continue

        panel_type = None
        panel_base = None
        panel_name = None
        if panel is not None:
            ptype = pdict.get("type")
            panel_type = ptype.value if hasattr(ptype, "value") else (str(ptype) if ptype else None)
            panel_base = pdict.get("base_url") or pdict.get("url")
            panel_name = pdict.get("name")
        # Never touch deferred/unloaded attrs (async lazy-load → MissingGreenlet).
        links_loaded = "links" in adict
        raw_links: list = []
        if links_loaded and not is_deleted:
            raw_links = [x for x in (adict.get("links") or []) if x]
        aid = int(adict.get("id") or 0)
        meta = share_meta.get(aid) if share_meta and aid else None
        if meta is not None:
            has_share, cfg_n = bool(meta[0]), int(meta[1] or 0)
        elif links_loaded:
            share_rows = [
                x for x in raw_links if x and is_share_link(str(x))
            ]
            has_share = bool(share_rows)
            cfg_n = len(share_rows)
        else:
            has_share = False
            cfg_n = 0
        # Never surface stale error_message on healthy accounts (UI false positives)
        err = (
            adict.get("error_message")
            if status_val == PanelAccountStatus.error
            else None
        )
        gid = adict.get("group_id")
        panel_accounts.append(
            PanelAccountOut(
                id=adict.get("id"),
                panel_id=adict.get("panel_id"),
                group_id=int(gid) if gid is not None else None,
                panel_name=panel_name,
                panel_type=panel_type,
                panel_base_url=str(panel_base).rstrip("/") if panel_base else None,
                panel_is_active=panel_active,
                remote_username=adict.get("remote_username"),
                status=status_val,
                # List view: omit URLs — has_share_link / configs_count carry truth.
                links=list(raw_links) if include_links else [],
                error_message=err,
                used_traffic=used,
                coeff_anchor_raw=int(adict.get("coeff_anchor_raw") or 0),
                coeff_anchor_weighted=int(adict.get("coeff_anchor_weighted") or 0),
                applied_coefficient=adict.get("applied_coefficient"),
                last_sync_at=adict.get("last_sync_at"),
                configs_count=cfg_n,
                has_share_link=has_share,
            )
        )

    groups = list(user.__dict__.get("groups") or [])
    created_at = user.__dict__.get("created_at")
    proxies: dict = {}
    if include_links:
        psettings = user.__dict__.get("proxy_settings") or {}
        proxies = (psettings.get("proxies") if isinstance(psettings, dict) else None) or (
            psettings if isinstance(psettings, dict) else {}
        )

    return UserResponse(
        username=user.username,
        display_name=(user.display_name or None),
        status=user.status,
        used_traffic=user.used_traffic,
        lifetime_used_traffic=user.lifetime_used_traffic,
        data_limit=user.data_limit,
        expire=user.expire or 0,
        on_hold_expire_duration=int(getattr(user, "on_hold_expire_duration", 0) or 0),
        include_promo_first=bool(getattr(user, "include_promo_first", True)),
        data_limit_reset_strategy=user.data_limit_reset_strategy,
        note=user.note,
        created_at=created_at,
        links=links,
        subscription_url=subscription_url(user),
        proxies=proxies,
        # Preserve membership order (selection order → subscription order)
        group_ids=[int(g.id) for g in groups],
        source_category_id=getattr(user, "source_category_id", None),
        panel_accounts=panel_accounts,
    )
