import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_admin
from app.core.database import get_db
from app.models.group import Group, UserGroup
from app.models.template import UserTemplate
from app.models.user import PanelAccount, User, UserStatus, generate_sub_token
from app.schemas.user import UserCreate, UserFromTemplate, UserModify, UserResponse, UsersListResponse
from app.services import orchestrator
from app.services.users import load_user, to_user_response

router = APIRouter(tags=["User"])


def _username_exists_error(username: str) -> HTTPException:
    """409 for bots/UI — code + Persian message."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "USERNAME_EXISTS",
            "message": "این یوزرنیم قبلاً ثبت شده است",
            "username": username,
        },
    )


async def _attach_groups(db: AsyncSession, user: User, group_ids: list[int]) -> None:
    """Link user↔groups via UserGroup rows — never assign user.groups (async lazy-load 500)."""
    result = await db.execute(select(Group.id).where(Group.id.in_(group_ids)))
    found = {row[0] for row in result.all()}
    if found != set(group_ids):
        raise HTTPException(
            status_code=400,
            detail={"code": "GROUP_NOT_FOUND", "message": "یک یا چند گروه پیدا نشد"},
        )
    await db.execute(delete(UserGroup).where(UserGroup.user_id == user.id))
    for gid in group_ids:
        db.add(UserGroup(user_id=user.id, group_id=gid))
    await db.flush()


@router.post("/api/user", response_model=UserResponse, status_code=status.HTTP_200_OK)
async def create_user(
    body: UserCreate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    existing = await db.execute(select(User).where(User.username == body.username))
    if existing.scalar_one_or_none():
        raise _username_exists_error(body.username)

    if not body.group_ids:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "GROUP_REQUIRED",
                "message": "حداقل یک گروه باید انتخاب شود",
            },
        )

    display = (body.display_name or "").strip() or None
    user = User(
        username=body.username,
        display_name=display,
        status=body.status,
        data_limit=body.data_limit or 0,
        expire=body.expire or 0,
        note=body.note,
        data_limit_reset_strategy=body.data_limit_reset_strategy,
        proxy_settings={
            "proxies": body.proxies or {},
            **(body.proxy_settings or {}),
            "inbounds": body.inbounds or {},
        },
        sub_token=generate_sub_token(),
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise _username_exists_error(body.username) from exc

    await _attach_groups(db, user, body.group_ids)

    panels = await orchestrator.resolve_panels_for_groups(db, body.group_ids)
    if not panels:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "NO_ACTIVE_PANELS",
                "message": "گروه‌های انتخاب‌شده پنل فعالی ندارند",
            },
        )

    try:
        await orchestrator.provision_user_on_panels(db, user, panels)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _username_exists_error(body.username) from exc
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        raise HTTPException(
            status_code=502,
            detail={
                "code": "PANEL_PROVISION_FAILED",
                "message": f"خطا هنگام ساخت روی پنل‌ها: {exc}",
            },
        ) from exc

    # Fresh load after commit — avoid stale/lazy relationships (MissingGreenlet)
    db.expire_all()
    user = await load_user(db, body.username)
    if not user:
        raise HTTPException(status_code=500, detail="کاربر ساخته شد ولی قابل خواندن نیست")
    try:
        return to_user_response(user)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"کاربر ساخته شد ولی پاسخ API خطا داد: {exc}",
        ) from exc


@router.get("/api/user/{username}", response_model=UserResponse)
async def get_user(
    username: str,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    user = await load_user(db, username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await orchestrator.refresh_usage(db, user, force=True)
    changed = await orchestrator.enforce_user_quota(db, user)
    if await orchestrator.should_push_remotes(
        db, user, status_changed=changed, shrink_shared_caps=True
    ):
        await orchestrator.sync_modify_user(db, user)
    await db.commit()
    user = await load_user(db, username)
    return to_user_response(user)  # type: ignore[arg-type]


@router.put("/api/user/{username}", response_model=UserResponse)
async def modify_user(
    username: str,
    body: UserModify,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    user = await load_user(db, username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if body.clear_display_name:
        user.display_name = None
    elif body.display_name is not None:
        user.display_name = body.display_name.strip() or None
    if body.status is not None:
        user.status = body.status
    if body.data_limit is not None:
        user.data_limit = body.data_limit
    if body.expire is not None:
        user.expire = body.expire
    if body.note is not None:
        user.note = body.note
    if body.data_limit_reset_strategy is not None:
        user.data_limit_reset_strategy = body.data_limit_reset_strategy
    if body.proxies is not None or body.proxy_settings is not None:
        ps = dict(user.proxy_settings or {})
        if body.proxies is not None:
            ps["proxies"] = body.proxies
        if body.proxy_settings is not None:
            ps.update(body.proxy_settings)
        user.proxy_settings = ps
    if body.group_ids is not None:
        await _attach_groups(db, user, body.group_ids)
        # provision any new panels
        panels = await orchestrator.resolve_panels_for_groups(db, body.group_ids)
        existing_panel_ids = {a.panel_id for a in user.panel_accounts}
        new_panels = [(p, c) for p, c in panels if p.id not in existing_panel_ids]
        if new_panels:
            await orchestrator.provision_user_on_panels(db, user, new_panels)

    # After admin re-enable, re-apply expire/limit before pushing remotes
    if body.status == UserStatus.active:
        await orchestrator.enforce_user_quota(db, user)

    await orchestrator.sync_modify_user(db, user)
    await db.commit()
    user = await load_user(db, username)
    return to_user_response(user)  # type: ignore[arg-type]


@router.delete("/api/user/{username}")
async def delete_user(
    username: str,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    user = await load_user(db, username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Always remove from Astra after best-effort remote deletes.
    # Broken/slow panels must not trap the user forever in the sales panel.
    results = await orchestrator.sync_delete_user(db, user)
    await db.delete(user)
    await db.commit()
    soft = [r for r in results if r.get("soft") and r.get("error")]
    return {
        "detail": "User deleted",
        "panels": results,
        "warnings": [
            f"{r.get('panel_name') or r.get('panel_id')}: {r.get('error')}"
            for r in soft
        ],
    }


@router.post("/api/user/{username}/reset", response_model=UserResponse)
async def reset_user_traffic(
    username: str,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Reset used traffic in Astra and on every remote panel for this user."""
    user = await load_user(db, username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    results = await orchestrator.reset_user_traffic_on_panels(db, user)
    await db.commit()
    user = await load_user(db, username)
    failed = [r for r in results if not r.get("ok")]
    if failed and user:
        # Still return user (local reset done) but surface soft warning via header-friendly detail
        # Keep 200 so UI can refresh; message is logged for admin toast via query invalidate
        parts = [f"{r.get('panel_name')}: {r.get('error')}" for r in failed]
        logger = __import__("logging").getLogger("astra.users")
        logger.warning("reset partial user=%s: %s", username, " | ".join(parts))
    return to_user_response(user)  # type: ignore[arg-type]


@router.post("/api/user/{username}/revoke_sub", response_model=UserResponse)
async def revoke_sub(
    username: str,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    user = await load_user(db, username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.sub_token = generate_sub_token()
    await db.commit()
    user = await load_user(db, username)
    return to_user_response(user)  # type: ignore[arg-type]


@router.post("/api/user/{username}/retry", response_model=UserResponse)
async def retry_provision(
    username: str,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    user = await load_user(db, username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await orchestrator.retry_failed_accounts(db, user)
    await db.commit()
    user = await load_user(db, username)
    return to_user_response(user)  # type: ignore[arg-type]


@router.get("/api/users", response_model=UsersListResponse)
async def list_users(
    offset: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    search: str | None = None,
    group_id: int | None = Query(None, ge=1),
    refresh: bool = Query(
        False,
        description="Pull live usage from remote panels for this page (read-only; no remote push)",
    ),
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    q = select(User).options(
        selectinload(User.groups),
        selectinload(User.panel_accounts).selectinload(PanelAccount.panel),
    )
    count_q = select(func.count(User.id))
    if search:
        q = q.where(User.username.ilike(f"%{search}%"))
        count_q = count_q.where(User.username.ilike(f"%{search}%"))
    if group_id is not None:
        q = q.join(UserGroup, UserGroup.user_id == User.id).where(UserGroup.group_id == group_id)
        count_q = count_q.join(UserGroup, UserGroup.user_id == User.id).where(
            UserGroup.group_id == group_id
        )
    total = (await db.execute(count_q)).scalar() or 0
    result = await db.execute(q.order_by(User.id.desc()).offset(offset).limit(limit))
    users = list(result.scalars().unique().all())

    if refresh and users:
        # Read usage only — never push modify on list refresh (was extremely slow)
        await orchestrator.refresh_users_usage_batch(
            db, users, force=True, concurrency=10, push_remotes=False
        )
        await db.commit()
        ids = [u.id for u in users]
        result = await db.execute(
            select(User)
            .where(User.id.in_(ids))
            .options(
                selectinload(User.groups),
                selectinload(User.panel_accounts).selectinload(PanelAccount.panel),
            )
            .order_by(User.id.desc())
        )
        by_id = {u.id: u for u in result.scalars().unique().all()}
        users = [by_id[i] for i in ids if i in by_id]

    return UsersListResponse(
        users=[to_user_response(u, include_links=False) for u in users],
        total=total,
    )


@router.post("/api/user/from_template", response_model=UserResponse)
async def create_from_template(
    body: UserFromTemplate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    tpl = await db.get(UserTemplate, body.user_template_id)
    if not tpl or tpl.is_disabled:
        raise HTTPException(status_code=404, detail="Template not found or disabled")

    username = f"{tpl.username_prefix or ''}{body.username}{tpl.username_suffix or ''}"
    expire = 0
    if tpl.expire_duration:
        expire = int(time.time()) + int(tpl.expire_duration)

    create_body = UserCreate(
        username=username,
        status=UserStatus(tpl.status) if tpl.status in UserStatus.__members__ else UserStatus.active,
        data_limit=tpl.data_limit,
        expire=expire,
        note=body.note or tpl.note,
        group_ids=list(tpl.group_ids or []),
        data_limit_reset_strategy=tpl.data_limit_reset_strategy,
        proxy_settings=tpl.extra_settings or {},
    )
    return await create_user(create_body, db, _admin)
