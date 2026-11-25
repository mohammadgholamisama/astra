from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select

from app.models.user import PanelAccountStatus, User
from app.schemas.user import PanelAccountOut, UserResponse
from app.services.site_settings import get_sub_base_url


def subscription_url(user: User) -> str:
    base = get_sub_base_url().rstrip("/")
    return f"{base}/sub/{user.sub_token}"


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


def to_user_response(user: User, *, include_links: bool = True) -> UserResponse:
    """Serialize user for API.

    include_links=False for list endpoints — cuts payload size a lot when
    many users each have large share-link arrays. List queries may ``defer``
    ``links``; never touch deferred attrs (async lazy-load is unsafe).
    """
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
        if not include_links or "links" not in adict or is_deleted:
            raw_links: list = []
        else:
            raw_links = [x for x in (adict.get("links") or []) if x]
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
                # List view: keep count only (placeholder) — avoid shipping huge URLs
                links=list(raw_links) if include_links else (["x"] * len(raw_links)),
                error_message=err,
                used_traffic=used,
                coeff_anchor_raw=int(adict.get("coeff_anchor_raw") or 0),
                coeff_anchor_weighted=int(adict.get("coeff_anchor_weighted") or 0),
                applied_coefficient=adict.get("applied_coefficient"),
                last_sync_at=adict.get("last_sync_at"),
                configs_count=len(raw_links),
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
