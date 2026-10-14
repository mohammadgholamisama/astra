from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select

from app.models.user import User
from app.schemas.user import PanelAccountOut, UserResponse
from app.services.site_settings import get_sub_base_url


def subscription_url(user: User) -> str:
    base = get_sub_base_url().rstrip("/")
    return f"{base}/sub/{user.sub_token}"


async def load_user(db: AsyncSession, username: str) -> User | None:
    from app.models.user import PanelAccount

    result = await db.execute(
        select(User)
        .where(User.username == username)
        .options(
            selectinload(User.groups),
            selectinload(User.panel_accounts).selectinload(PanelAccount.panel),
        )
    )
    return result.scalar_one_or_none()


async def load_user_by_token(db: AsyncSession, token: str) -> User | None:
    from app.models.user import PanelAccount

    result = await db.execute(
        select(User)
        .where(User.sub_token == token)
        .options(
            selectinload(User.groups),
            selectinload(User.panel_accounts).selectinload(PanelAccount.panel),
        )
    )
    return result.scalar_one_or_none()


def to_user_response(user: User, *, include_links: bool = True) -> UserResponse:
    """Serialize user for API.

    include_links=False for list endpoints — cuts payload size a lot when
    many users each have large share-link arrays.
    """
    links: list[str] = []
    seen: set[str] = set()
    accounts = list(user.__dict__.get("panel_accounts") or [])
    if include_links:
        for account in accounts:
            for link in account.links or []:
                if link and link not in seen:
                    seen.add(link)
                    links.append(link)

    panel_accounts = []
    for a in accounts:
        # Only use panel if already loaded — never trigger async lazy IO
        panel = a.__dict__.get("panel")
        panel_type = None
        if panel is not None:
            ptype = getattr(panel, "type", None)
            panel_type = ptype.value if hasattr(ptype, "value") else (str(ptype) if ptype else None)
        raw_links = [x for x in (a.links or []) if x]
        panel_accounts.append(
            PanelAccountOut(
                id=a.id,
                panel_id=a.panel_id,
                panel_name=getattr(panel, "name", None) if panel is not None else None,
                panel_type=panel_type,
                remote_username=a.remote_username,
                status=a.status,
                # List view: keep count only (placeholder) — avoid shipping huge URLs
                links=list(raw_links) if include_links else (["x"] * len(raw_links)),
                error_message=a.error_message,
                used_traffic=a.used_traffic,
                coeff_anchor_raw=int(getattr(a, "coeff_anchor_raw", 0) or 0),
                coeff_anchor_weighted=int(getattr(a, "coeff_anchor_weighted", 0) or 0),
                applied_coefficient=getattr(a, "applied_coefficient", None),
                last_sync_at=a.last_sync_at,
            )
        )

    groups = list(user.__dict__.get("groups") or [])
    created_at = user.__dict__.get("created_at") or user.created_at
    proxies: dict = {}
    if include_links:
        proxies = (user.proxy_settings or {}).get("proxies") or user.proxy_settings or {}

    return UserResponse(
        username=user.username,
        display_name=(user.display_name or None),
        status=user.status,
        used_traffic=user.used_traffic,
        lifetime_used_traffic=user.lifetime_used_traffic,
        data_limit=user.data_limit,
        expire=user.expire or 0,
        data_limit_reset_strategy=user.data_limit_reset_strategy,
        note=user.note,
        created_at=created_at,
        links=links,
        subscription_url=subscription_url(user),
        proxies=proxies,
        group_ids=[g.id for g in groups],
        panel_accounts=panel_accounts,
    )
