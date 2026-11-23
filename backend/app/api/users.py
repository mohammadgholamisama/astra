import asyncio
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer, load_only, selectinload

from app.api.deps import get_current_admin, oauth2_scheme
from app.core.database import get_db
from app.models.group import Group, UserGroup
from app.models.panel import Panel
from app.models.template import UserTemplate
from app.models.user import PanelAccount, PanelAccountStatus, User, UserStatus, generate_sub_token
from app.schemas.user import (
    UserBulkCreate,
    UserBulkCreateResponse,
    UserBulkFail,
    UserCheckUsagePanel,
    UserCheckUsageResponse,
    UserCreate,
    UserFixSubResponse,
    UserFromTemplate,
    UserModify,
    UserResponse,
    UserRetryErrorsResponse,
    UsersListCounts,
    UsersListResponse,
)
from app.services import orchestrator
from app.services.users import load_user, to_user_response

router = APIRouter(tags=["User"])

# Bot/API clients often stampede /api/users?search=&limit=100 — cap concurrency
# so interactive panel pages keep DB pool headroom.
_USERS_LIST_SEM = asyncio.Semaphore(6)


def _hold_required_error() -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={
            "code": "HOLD_DURATION_REQUIRED",
            "message": "برای شروع از اولین استفاده باید روز اعتبار بیشتر از صفر باشد",
        },
    )


def _normalize_on_hold(
    status: UserStatus,
    expire: int | None,
    duration: int | None,
) -> tuple[UserStatus, int, int]:
    """Map create/edit fields to (status, expire_ts, on_hold_duration_seconds).

    on_hold: expire stays 0 until first traffic; duration is the purchased days.
    """
    if status != UserStatus.on_hold:
        return status, int(expire or 0), 0
    dur = int(duration or 0)
    if dur <= 0:
        now = int(time.time())
        exp = int(expire or 0)
        if exp > now:
            dur = exp - now
    if dur <= 0:
        raise _hold_required_error()
    return UserStatus.on_hold, 0, dur


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


def _user_created_recently(user: User, *, window_sec: int = 600) -> bool:
    """True when row was inserted in the last ``window_sec`` (idempotent retry)."""
    created = getattr(user, "created_at", None)
    if created is None:
        return False
    if getattr(created, "tzinfo", None) is None:
        created = created.replace(tzinfo=timezone.utc)
    try:
        age = (datetime.now(timezone.utc) - created).total_seconds()
    except Exception:  # noqa: BLE001
        return False
    return 0 <= age <= float(window_sec)


async def _bg_finish_create_provision(username: str, group_ids: list[int]) -> None:
    """Finish remotes after create returned — must not block the HTTP response."""
    from app.core.database import AsyncSessionLocal

    log = __import__("logging").getLogger("astra.users")
    try:
        async with AsyncSessionLocal() as db:
            user = await load_user(db, username)
            if user is None:
                return
            panels = await orchestrator.resolve_panels_for_groups(db, group_ids)
            if panels:
                try:
                    await orchestrator.provision_users_now(
                        db, [user], panels, wait=55.0
                    )
                except Exception:  # noqa: BLE001
                    log.exception("bg create provision failed user=%s", username)
                    try:
                        await db.rollback()
                    except Exception:  # noqa: BLE001
                        pass
            try:
                await db.commit()
            except Exception:  # noqa: BLE001
                try:
                    await db.rollback()
                except Exception:  # noqa: BLE001
                    pass
        try:
            await orchestrator.heal_user_coverage_pool_safe(
                int(user.id), wait=20.0, include_empty_links=True
            )
        except Exception:  # noqa: BLE001
            log.debug("bg create heal user=%s failed", username, exc_info=True)
    except Exception:  # noqa: BLE001
        log.exception("bg finish create failed user=%s", username)


async def _resolve_source_category(
    db: AsyncSession, category_id: int | None
) -> int | None:
    """Validate category exists; return id or None."""
    if category_id is None:
        return None
    from app.models.group import GroupCategory

    cat = await db.get(GroupCategory, int(category_id))
    if not cat:
        raise HTTPException(
            status_code=400,
            detail={"code": "CATEGORY_NOT_FOUND", "message": "دسته پیدا نشد"},
        )
    return int(cat.id)


async def _attach_groups(db: AsyncSession, user: User, group_ids: list[int]) -> None:
    """Replace user↔groups in the given order (order = subscription config order)."""
    # Preserve selection order; drop dupes while keeping first occurrence
    ordered: list[int] = []
    seen: set[int] = set()
    for raw in group_ids:
        try:
            gid = int(raw)
        except (TypeError, ValueError):
            continue
        if gid in seen:
            continue
        seen.add(gid)
        ordered.append(gid)
    result = await db.execute(select(Group.id).where(Group.id.in_(ordered or [-1])))
    found = {row[0] for row in result.all()}
    if found != set(ordered):
        raise HTTPException(
            status_code=400,
            detail={"code": "GROUP_NOT_FOUND", "message": "یک یا چند گروه پیدا نشد"},
        )
    await db.execute(delete(UserGroup).where(UserGroup.user_id == user.id))
    for i, gid in enumerate(ordered):
        db.add(UserGroup(user_id=user.id, group_id=gid, sort_order=i))
    await db.flush()


async def _add_groups_only(db: AsyncSession, user: User, group_ids: list[int]) -> list[int]:
    """Append groups to the user — never remove existing memberships.

    New groups are appended after existing ones (keeps prior subscription order).
    Returns newly added group ids (empty if nothing new).
    """
    wanted: list[int] = []
    seen: set[int] = set()
    for raw in group_ids:
        try:
            gid = int(raw)
        except (TypeError, ValueError):
            continue
        if gid in seen:
            continue
        seen.add(gid)
        wanted.append(gid)
    if not wanted:
        return []
    result = await db.execute(select(Group.id).where(Group.id.in_(wanted)))
    found = {row[0] for row in result.all()}
    if found != set(wanted):
        raise HTTPException(
            status_code=400,
            detail={"code": "GROUP_NOT_FOUND", "message": "یک یا چند گروه پیدا نشد"},
        )
    existing = await db.execute(
        select(UserGroup)
        .where(UserGroup.user_id == user.id)
        .order_by(UserGroup.sort_order.asc(), UserGroup.id.asc())
    )
    rows = list(existing.scalars().all())
    have = {int(r.group_id) for r in rows}
    next_ord = (max((int(r.sort_order or 0) for r in rows), default=-1) + 1) if rows else 0
    added: list[int] = []
    for gid in wanted:
        if gid in have:
            continue
        db.add(UserGroup(user_id=user.id, group_id=gid, sort_order=next_ord))
        next_ord += 1
        added.append(gid)
    if added:
        await db.flush()
    return added


@router.post("/api/user", response_model=UserResponse, status_code=status.HTTP_200_OK)
async def create_user(
    body: UserCreate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Create Astra user + kick remotes.

    Critical: local row is committed BEFORE remote HTTP. The response MUST return
    200 once that commit succeeds — waiting on every panel (up to ~90s) made the
    edge/nginx return 500/502 while the user already existed, so retry showed
    «قبلاً ثبت شده». Short sync provision + background finish + idempotent retry.
    """
    log = __import__("logging").getLogger("astra.users")

    existing = await db.execute(select(User).where(User.username == body.username))
    prior = existing.scalar_one_or_none()
    if prior is not None:
        # Idempotent: first request timed out after commit → retry returns the user
        if _user_created_recently(prior):
            user = await load_user(db, body.username)
            if user is not None:
                try:
                    asyncio.create_task(
                        _bg_finish_create_provision(
                            body.username, list(body.group_ids or [])
                        ),
                        name=f"astra-create-idempotent-{body.username}",
                    )
                except Exception:  # noqa: BLE001
                    pass
                return to_user_response(user)
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
    user_status, expire, hold_dur = _normalize_on_hold(
        body.status, body.expire, body.on_hold_expire_duration
    )
    source_cat = await _resolve_source_category(db, body.category_id)
    user = User(
        username=body.username,
        display_name=display,
        status=user_status,
        data_limit=body.data_limit or 0,
        expire=expire,
        on_hold_expire_duration=hold_dur,
        include_promo_first=bool(body.include_promo_first),
        note=body.note,
        data_limit_reset_strategy=body.data_limit_reset_strategy,
        proxy_settings={
            "proxies": body.proxies or {},
            **(body.proxy_settings or {}),
            "inbounds": body.inbounds or {},
        },
        sub_token=generate_sub_token(),
        source_category_id=source_cat,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    try:
        # SQLite may be briefly busy while usage-sync commits — retry flush.
        for attempt in range(8):
            try:
                await db.flush()
                break
            except OperationalError as exc:
                msg = str(exc).lower()
                if "locked" not in msg and "busy" not in msg:
                    raise
                if attempt >= 7:
                    raise
                await asyncio.sleep(0.2 * (attempt + 1))
    except IntegrityError as exc:
        await db.rollback()
        # Race: concurrent create committed first — treat recent row as success
        raced = await load_user(db, body.username)
        if raced is not None and _user_created_recently(raced):
            return to_user_response(raced)
        raise _username_exists_error(body.username) from exc
    except OperationalError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DB_BUSY",
                "message": "دیتابیس موقتاً شلوغ است — چند ثانیه بعد دوباره بسازید",
            },
        ) from exc

    await _attach_groups(db, user, body.group_ids)

    panels = await orchestrator.resolve_panels_for_groups(db, body.group_ids)
    if not panels:
        await db.rollback()
        raise HTTPException(
            status_code=400,
            detail={
                "code": "NO_ACTIVE_PANELS",
                "message": "گروه‌های انتخاب‌شده پنل فعالی ندارند",
            },
        )

    # Commit user+groups BEFORE panel HTTP so the write lock is not held
    # across remote I/O (that caused locked DB / opaque 500s).
    try:
        for attempt in range(6):
            try:
                await db.commit()
                break
            except OperationalError as exc:
                msg = str(exc).lower()
                if "locked" not in msg and "busy" not in msg:
                    raise
                if attempt >= 5:
                    raise
                await asyncio.sleep(0.25 * (attempt + 1))
    except IntegrityError as exc:
        await db.rollback()
        raced = await load_user(db, body.username)
        if raced is not None and _user_created_recently(raced):
            return to_user_response(raced)
        raise _username_exists_error(body.username) from exc
    except OperationalError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DB_BUSY",
                "message": "دیتابیس موقتاً شلوغ است — چند ثانیه بعد دوباره بسازید",
            },
        ) from exc

    # From here the user EXISTS — never return 5xx for provision/serialize issues.
    # Do NOT wait on remotes here: «همه گروه‌ها» can be 30+ panels and the
    # admin UI / edge aborts while Astra already committed → false timeout.
    # All panel creates run concurrently in background.
    try:
        asyncio.create_task(
            _bg_finish_create_provision(body.username, list(body.group_ids)),
            name=f"astra-create-finish-{body.username}",
        )
    except Exception:  # noqa: BLE001
        pass

    db.expire_all()
    try:
        user = await load_user(db, body.username)
    except Exception:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        user = None
    if not user:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "CREATE_READBACK",
                "message": "کاربر ثبت شد ولی خواندن پاسخ طول کشید — لیست را رفرش کنید",
            },
        )
    try:
        return to_user_response(user)
    except Exception as exc:  # noqa: BLE001
        log.exception("create serialize failed user=%s", body.username)
        # Hand-built success body — user is already in Astra
        from app.services.users import subscription_url

        return UserResponse(
            username=str(user.username),
            display_name=(getattr(user, "display_name", None) or None),
            status=user.status,
            used_traffic=int(getattr(user, "used_traffic", 0) or 0),
            lifetime_used_traffic=int(getattr(user, "lifetime_used_traffic", 0) or 0),
            data_limit=int(getattr(user, "data_limit", 0) or 0),
            expire=int(getattr(user, "expire", 0) or 0) or 0,
            on_hold_expire_duration=int(
                getattr(user, "on_hold_expire_duration", 0) or 0
            ),
            include_promo_first=bool(getattr(user, "include_promo_first", True)),
            data_limit_reset_strategy=str(
                getattr(user, "data_limit_reset_strategy", None) or "no_reset"
            ),
            note=getattr(user, "note", None),
            created_at=getattr(user, "created_at", None),
            links=[],
            subscription_url=subscription_url(user),
            proxies={},
            group_ids=list(body.group_ids),
            source_category_id=getattr(user, "source_category_id", None),
            panel_accounts=[],
        )


@router.post("/api/users/bulk", response_model=UserBulkCreateResponse)
async def create_users_bulk(
    body: UserBulkCreate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Create ``count`` users as ``{prefix}_1`` … ``{prefix}_{count}``.

    One local transaction for all rows, then every group×panel remote is
    created in a single parallel wave. Dead panels become account.error;
    the Astra user always stays.
    """
    import logging

    log = logging.getLogger("astra.users.bulk")

    if not body.group_ids:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "GROUP_REQUIRED",
                "message": "حداقل یک گروه باید انتخاب شود",
            },
        )

    prefix = (body.username_prefix or "").strip()
    if not prefix:
        raise HTTPException(
            status_code=400,
            detail={"code": "PREFIX_REQUIRED", "message": "پیشوند یوزرنیم لازم است"},
        )

    names: list[str] = []
    for i in range(1, int(body.count) + 1):
        uname = f"{prefix}_{i}"
        if len(uname) > 32:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "USERNAME_TOO_LONG",
                    "message": f"یوزرنیم «{uname}» بیشتر از ۳۲ کاراکتر می‌شود — پیشوند کوتاه‌تر بگذارید",
                },
            )
        names.append(uname)

    if len(set(names)) != len(names):
        raise HTTPException(
            status_code=400,
            detail={"code": "BAD_PREFIX", "message": "پیشوند یوزرنیم نامعتبر است"},
        )

    existing = await db.execute(select(User.username).where(User.username.in_(names)))
    taken = {row[0] for row in existing.all()}
    if taken:
        sample = ", ".join(sorted(taken)[:5])
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "USERNAME_EXISTS",
                "message": f"این یوزرنیم‌ها از قبل وجود دارند: {sample}",
                "usernames": sorted(taken),
            },
        )

    panels = await orchestrator.resolve_panels_for_groups(db, body.group_ids)
    if not panels:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "NO_ACTIVE_PANELS",
                "message": "گروه‌های انتخاب‌شده پنل فعالی ندارند",
            },
        )

    display = (body.display_name or "").strip() or None
    # Do NOT name this ``status`` — shadows fastapi.status and can 500 later.
    user_status, expire, hold_dur = _normalize_on_hold(
        body.status, body.expire, body.on_hold_expire_duration
    )
    source_cat = await _resolve_source_category(db, body.category_id)
    now = datetime.now(timezone.utc)
    # Snapshot panel targets once — (panel_id, config, group_id) for fast reload
    panel_specs: list[tuple[int, dict, int]] = [
        (int(p.id), dict(cfg or {}), int(gid)) for p, cfg, gid in panels
    ]
    panel_id_set = list({pid for pid, _, _ in panel_specs})

    failed: list[UserBulkFail] = []
    proxy_settings = {
        "proxies": body.proxies or {},
        **(body.proxy_settings or {}),
        "inbounds": body.inbounds or {},
    }
    include_promo = bool(getattr(body, "include_promo_first", True))

    ordered_gids: list[int] = []
    seen_g: set[int] = set()
    for raw in body.group_ids:
        try:
            gid = int(raw)
        except (TypeError, ValueError):
            continue
        if gid in seen_g:
            continue
        seen_g.add(gid)
        ordered_gids.append(gid)
    found_g = {
        int(r[0])
        for r in (
            await db.execute(select(Group.id).where(Group.id.in_(ordered_gids or [-1])))
        ).all()
    }
    if found_g != set(ordered_gids):
        raise HTTPException(
            status_code=400,
            detail={"code": "GROUP_NOT_FOUND", "message": "یک یا چند گروه پیدا نشد"},
        )

    users: list[User] = []
    try:
        for uname in names:
            user = User(
                username=uname,
                display_name=display,
                status=user_status,
                data_limit=body.data_limit or 0,
                expire=expire,
                on_hold_expire_duration=hold_dur,
                include_promo_first=include_promo,
                note=body.note,
                data_limit_reset_strategy=body.data_limit_reset_strategy,
                proxy_settings=proxy_settings,
                sub_token=generate_sub_token(),
                source_category_id=source_cat,
                created_at=now,
            )
            db.add(user)
            users.append(user)
        await db.flush()
        for user in users:
            for i, gid in enumerate(ordered_gids):
                db.add(UserGroup(user_id=user.id, group_id=gid, sort_order=i))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "USERNAME_EXISTS",
                "message": "یکی از یوزرنیم‌ها هم‌زمان ساخته شد — دوباره تلاش کنید",
            },
        )
    except OperationalError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DB_BUSY",
                "message": "دیتابیس موقتاً شلوغ است — چند ثانیه بعد دوباره بسازید",
            },
        ) from exc

    created_names = [u.username for u in users]
    db.expire_all()
    loaded = await db.execute(
        select(User)
        .where(User.username.in_(created_names))
        .options(
            selectinload(User.groups),
            selectinload(User.panel_accounts).selectinload(PanelAccount.panel),
        )
    )
    users = list(loaded.scalars().unique().all())
    by_name = {u.username: u for u in users}
    users = [by_name[n] for n in created_names if n in by_name]

    rows = await db.execute(select(Panel).where(Panel.id.in_(panel_id_set or [-1])))
    by_id = {int(p.id): p for p in rows.scalars().all()}
    local_panels = [
        (by_id[pid], cfg, gid)
        for pid, cfg, gid in panel_specs
        if pid in by_id and bool(getattr(by_id[pid], "is_active", True))
    ]
    if users and local_panels:
        try:
            await orchestrator.provision_users_now(db, users, local_panels)
        except Exception:  # noqa: BLE001
            log.exception("bulk provision failed")
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
        try:
            asyncio.create_task(
                orchestrator.heal_failed_panel_accounts_batch(
                    limit_users=max(10, len(created_names))
                ),
                name="astra-heal-after-bulk",
            )
        except Exception:  # noqa: BLE001
            pass

    db.expire_all()
    created: list[UserResponse] = []
    try:
        loaded = await db.execute(
            select(User)
            .where(User.username.in_(created_names))
            .options(
                selectinload(User.groups),
                selectinload(User.panel_accounts).selectinload(PanelAccount.panel),
            )
        )
        by_name = {u.username: u for u in loaded.scalars().unique().all()}
        for uname in created_names:
            user = by_name.get(uname)
            if not user:
                failed.append(
                    UserBulkFail(username=uname, message="کاربر ساخته شد ولی قابل خواندن نیست")
                )
                continue
            try:
                created.append(to_user_response(user, include_links=True))
            except Exception as exc:  # noqa: BLE001
                log.warning("bulk serialize user=%s: %s", uname, exc)
                created.append(
                    UserResponse(
                        username=user.username,
                        display_name=user.display_name,
                        status=user.status,
                        used_traffic=int(user.used_traffic or 0),
                        lifetime_used_traffic=int(user.lifetime_used_traffic or 0),
                        data_limit=int(user.data_limit or 0),
                        expire=int(user.expire or 0),
                        on_hold_expire_duration=int(
                            getattr(user, "on_hold_expire_duration", 0) or 0
                        ),
                        include_promo_first=bool(
                            getattr(user, "include_promo_first", True)
                        ),
                        note=user.note,
                        subscription_url="",
                        group_ids=body.group_ids,
                        source_category_id=getattr(user, "source_category_id", None),
                    )
                )
    except Exception:  # noqa: BLE001
        log.exception("bulk final load failed")

    return UserBulkCreateResponse(
        created=created,
        failed=failed,
        total_requested=len(names),
    )


@router.get("/api/user/{username}", response_model=UserResponse)
async def get_user(
    username: str,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    user = await load_user(db, username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # Fast path: recount from panel_accounts already in DB (no remote HTTP on
    # the request session — that caused PendingRollback / used_traffic stuck at 0).
    try:
        await orchestrator.recount_user_used_traffic(db, user)
        await orchestrator.enforce_user_quota(db, user)
        await db.commit()
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger("astra.users").exception(
            "get_user recount soft-fail user=%s", username
        )
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
    # Live panel scrape in background so the dialog stays instant.
    try:
        asyncio.create_task(
            orchestrator.refresh_usage_pool_safe(
                int(user.id), force=True, quick=True
            ),
            name=f"astra-get-user-usage-{username[:24]}",
        )
    except Exception:  # noqa: BLE001
        pass
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
        incoming = body.status
        # Re-enable an unused on-hold user → keep waiting for first use
        if incoming == UserStatus.active:
            unused_hold = (
                int(getattr(user, "on_hold_expire_duration", 0) or 0) > 0
                and int(user.expire or 0) <= 0
                and int(user.used_traffic or 0) <= 0
                and user.status in (UserStatus.disabled, UserStatus.on_hold)
            )
            if unused_hold:
                incoming = UserStatus.on_hold
        if incoming == UserStatus.on_hold:
            dur = (
                body.on_hold_expire_duration
                if body.on_hold_expire_duration is not None
                else int(getattr(user, "on_hold_expire_duration", 0) or 0)
            )
            st, exp, hold_dur = _normalize_on_hold(incoming, body.expire, dur)
            user.status = st
            user.expire = exp
            user.on_hold_expire_duration = hold_dur
        else:
            user.status = incoming
    if body.data_limit is not None:
        user.data_limit = body.data_limit
    if body.expire is not None and user.status != UserStatus.on_hold:
        user.expire = body.expire
    if body.on_hold_expire_duration is not None and user.status == UserStatus.on_hold:
        st, exp, hold_dur = _normalize_on_hold(
            UserStatus.on_hold, 0, body.on_hold_expire_duration
        )
        user.status = st
        user.expire = exp
        user.on_hold_expire_duration = hold_dur
    if body.include_promo_first is not None:
        user.include_promo_first = bool(body.include_promo_first)
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
    groups_changed = False
    if body.clear_category:
        user.source_category_id = None
    elif body.category_id is not None:
        user.source_category_id = await _resolve_source_category(db, body.category_id)

    if body.group_ids is not None:
        if not body.group_ids:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "GROUP_REQUIRED",
                    "message": "کاربر باید حداقل یک گروه داشته باشد",
                },
            )

        ug = await db.execute(
            select(UserGroup.group_id)
            .where(UserGroup.user_id == user.id)
            .order_by(UserGroup.sort_order.asc(), UserGroup.id.asc())
        )
        old_order = [int(row[0]) for row in ug.all() if row[0] is not None]
        old_gids = set(old_order)

        # Normalize wanted ids (preserve order, drop dupes)
        wanted: list[int] = []
        seen: set[int] = set()
        for raw in body.group_ids:
            try:
                gid = int(raw)
            except (TypeError, ValueError):
                continue
            if gid in seen:
                continue
            seen.add(gid)
            wanted.append(gid)
        wanted_set = set(wanted)
        removed = sorted(old_gids - wanted_set)
        added = [g for g in wanted if g not in old_gids]
        # Skip heavy provision when groups (and order) are unchanged
        groups_changed = old_order != wanted

        if groups_changed:
            # Manual group edit without category_id → stop tracking category
            if body.category_id is None and not body.clear_category:
                user.source_category_id = None

            # Validate panels exist before rewriting membership
            panels = await orchestrator.resolve_panels_for_groups(db, wanted)
            if not panels:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "NO_ACTIVE_PANELS",
                        "message": "گروه‌های انتخاب‌شده هیچ پنل فعالی ندارند",
                    },
                )

            # Membership + local retire ONLY in this request session — never hold
            # the pool connection across remote create/delete (that closed the
            # connection → commit 500 / PendingRollbackError).
            await _attach_groups(db, user, wanted)
            if removed:
                removed_set = set(removed)
                for acc in list(user.panel_accounts or []):
                    gid = getattr(acc, "group_id", None)
                    if gid is None:
                        continue
                    if int(gid) not in removed_set:
                        continue
                    if acc.status == PanelAccountStatus.deleted:
                        continue
                    acc.status = PanelAccountStatus.deleted
                    # keep used_traffic / links banked for quota

            if (
                body.data_limit is not None
                or body.expire is not None
                or body.status is not None
            ):
                try:
                    await orchestrator.enforce_user_quota(db, user)
                except Exception:  # noqa: BLE001
                    pass

            await db.commit()
            uid = int(user.id)
            asyncio.create_task(
                orchestrator.apply_user_group_changes_background(
                    uid,
                    removed_group_ids=removed,
                    added_group_ids=added,
                ),
                name=f"astra-group-edit-{username}",
            )
            try:
                from app.api.subscription import invalidate_user_feed_cache

                invalidate_user_feed_cache(uid)
            except Exception:  # noqa: BLE001
                pass
            user = await load_user(db, username)
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            return to_user_response(user)  # type: ignore[arg-type]

    # Re-evaluate quota whenever volume / expire / status change.
    # Raising data_limit while status was "limited" must flip back to active
    # BEFORE sync_modify — otherwise remotes stay frozen at used_raw and the
    # new purchase never appears (especially Astra outbound panels).
    if (
        body.data_limit is not None
        or body.expire is not None
        or body.status is not None
    ):
        try:
            await orchestrator.enforce_user_quota(db, user)
        except Exception:  # noqa: BLE001
            pass

    # Commit local fields first; push ALL remotes (Astra + foreign) in background
    # so edit with many panels never times out the admin modal.
    uid = int(user.id)
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger("astra.users").exception(
            "modify_user commit soft-fail user=%s", username
        )
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
    try:
        asyncio.create_task(
            orchestrator.sync_modify_user_background(uid),
            name=f"astra-sync-remotes-{username}",
        )
    except Exception:  # noqa: BLE001
        pass
    if body.include_promo_first is not None:
        try:
            from app.api.subscription import invalidate_user_feed_cache

            invalidate_user_feed_cache(int(user.id))
        except Exception:  # noqa: BLE001
            pass
    try:
        user = await load_user(db, username)
    except Exception:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        user = await load_user(db, username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return to_user_response(user)  # type: ignore[arg-type]


@router.delete("/api/user/{username}")
async def delete_user(
    username: str,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Delete Astra user immediately; purge remotes in background.

    Holding the request DB session across 30+ panel HTTPs caused
    ``underlying connection is closed`` → HTTP 500 while the user stayed.
    """
    user = await load_user(db, username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    uid = int(user.id)
    uname = str(user.username or username)
    jobs = await orchestrator.snapshot_user_delete_jobs(db, user)
    panels_count = len(jobs)

    try:
        from app.api.subscription import invalidate_user_feed_cache

        invalidate_user_feed_cache(uid)
    except Exception:  # noqa: BLE001
        pass

    await db.delete(user)
    await db.commit()

    if jobs:
        try:
            asyncio.create_task(
                orchestrator.purge_user_remotes_background(jobs),
                name=f"astra-del-user-{uname[:24]}",
            )
        except RuntimeError:
            # No running loop edge-case — still deleted from Astra
            pass

    return {
        "detail": "User deleted",
        "panels_count": panels_count,
        "remote_bg": True,
        "skipped_count": 0,
        "skipped": [],
    }


@router.post("/api/user/{username}/check-usage", response_model=UserCheckUsageResponse)
async def check_user_usage(
    username: str,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Force live usage pull from every remote panel for one user.

    Admin «چک مصرف دقیق» — waits for remotes (not background-only) so the
    returned total matches پنل اصلی right now.
    """
    user = await load_user(db, username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    uid = int(user.id)
    used_before = int(user.used_traffic or 0)
    t0 = time.monotonic()
    try:
        orchestrator.mark_live_usage_priority(uid)
    except Exception:  # noqa: BLE001
        pass

    # Complete pull (not quick) so PasarGuard / slow remotes are not skipped.
    after = -1
    try:
        after, _activated, _changed, _st = await asyncio.wait_for(
            orchestrator.refresh_usage_pool_safe(
                uid, force=True, quick=False
            ),
            timeout=90.0,
        )
    except asyncio.TimeoutError:
        try:
            after = await orchestrator.recount_user_usage_pool_safe(uid)
        except Exception:  # noqa: BLE001
            after = -1
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail={
                "code": "USAGE_PULL_FAILED",
                "message": f"استعلام مصرف ناموفق: {exc}",
            },
        ) from exc

    user = await load_user(db, username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    used_after = int(user.used_traffic or 0)
    if after >= 0:
        used_after = max(used_after, int(after))

    panels_out: list[UserCheckUsagePanel] = []
    synced = 0
    for a in user.panel_accounts or []:
        st = getattr(a.status, "value", a.status)
        if st == "deleted":
            continue
        pan = a.__dict__.get("panel")
        ptype = getattr(pan, "type", None) if pan is not None else None
        ptype_s = (
            ptype.value if hasattr(ptype, "value") else (str(ptype) if ptype else None)
        )
        last = a.last_sync_at
        last_s = last.isoformat() if last is not None else None
        if last is not None:
            synced += 1
        panels_out.append(
            UserCheckUsagePanel(
                panel_id=int(a.panel_id),
                panel_name=getattr(pan, "name", None) if pan is not None else None,
                panel_type=ptype_s,
                remote_username=(a.remote_username or "").strip() or None,
                status=str(st or ""),
                used_raw=int(a.used_traffic or 0),
                last_sync_at=last_s,
            )
        )
    panels_out.sort(key=lambda r: (-int(r.used_raw or 0), r.panel_id))

    dt_ms = int((time.monotonic() - t0) * 1000)
    delta = used_after - used_before
    if delta > 0:
        msg = (
            f"مصرف به‌روز شد: {used_before} → {used_after} بایت "
            f"(+{delta}) از {len(panels_out)} اکانت"
        )
    elif used_after > 0:
        msg = (
            f"مصرف تایید شد: {used_after} بایت "
            f"({synced}/{len(panels_out)} پنل همگام)"
        )
    else:
        msg = (
            f"استعلام انجام شد — مصرف هنوز ۰ "
            f"({synced}/{len(panels_out)} پنل همگام در {dt_ms}ms)"
        )

    return UserCheckUsageResponse(
        username=str(user.username),
        used_before=used_before,
        used_after=used_after,
        data_limit=int(user.data_limit or 0),
        status=str(getattr(user.status, "value", user.status)),
        accounts=len(panels_out),
        synced=synced,
        dt_ms=dt_ms,
        message=msg,
        panels=panels_out,
        user=to_user_response(user),
    )


@router.post("/api/user/{username}/reset", response_model=UserResponse)
async def reset_user_traffic(
    username: str,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Reset used traffic fast: zero Astra locally, remotes in background.

    Waiting for every remote panel in the HTTP request caused browser/CF
    ``Failed to fetch`` on heavy users. UI must return immediately with zeros;
    concurrent remote resets continue in the background.
    """
    import logging

    from app.core.database import AsyncSessionLocal

    logger = logging.getLogger("astra.users")
    user = await load_user(db, username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user_id = int(user.id)

    # 1) Local zero immediately (what the admin sees) — must stay sub-second.
    user.lifetime_used_traffic = int(user.lifetime_used_traffic or 0) + int(
        user.used_traffic or 0
    )
    user.used_traffic = 0
    if user.status == UserStatus.limited:
        user.status = UserStatus.active
    acc_result = await db.execute(
        select(PanelAccount).where(PanelAccount.user_id == user_id)
    )
    for acc in acc_result.scalars().all():
        if acc.status == PanelAccountStatus.deleted:
            continue
        acc.used_traffic = 0
        acc.coeff_anchor_raw = 0
        acc.coeff_anchor_weighted = 0
    await db.commit()

    # 2) Astra + all remotes + re-enable/push caps — concurrent background.
    # Never await remotes on the HTTP path (CF/browser → Failed to fetch).
    async def _bg_full_reset() -> None:
        try:
            async with AsyncSessionLocal() as sdb:
                u = await load_user(sdb, username)
                if not u:
                    return
                results = await orchestrator.reset_user_traffic_on_panels(
                    sdb,
                    u,
                    prefer_types=None,
                    remote_timeout=10,
                    sync_after=True,
                    sync_remote_timeout=10,
                    concurrency=24,
                )
                u.used_traffic = 0
                if u.status == UserStatus.limited:
                    u.status = UserStatus.active
                await sdb.commit()
                failed = [r for r in results if not r.get("ok")]
                if failed:
                    logger.warning(
                        "reset remotes partial user=%s fail=%s/%s",
                        username,
                        len(failed),
                        len(results),
                    )
                    fail_ids = {
                        int(r["panel_id"])
                        for r in failed
                        if r.get("panel_id") is not None
                    }
                    if fail_ids:
                        async with AsyncSessionLocal() as rdb:
                            uu = await load_user(rdb, username)
                            if not uu:
                                return
                            await orchestrator.reset_user_traffic_on_panels(
                                rdb,
                                uu,
                                panel_ids=fail_ids,
                                remote_timeout=12,
                                sync_after=True,
                                sync_remote_timeout=12,
                                concurrency=16,
                            )
                            uu.used_traffic = 0
                            if uu.status == UserStatus.limited:
                                uu.status = UserStatus.active
                            await rdb.commit()
        except Exception:  # noqa: BLE001
            logger.exception("background reset remotes failed user=%s", username)

    asyncio.create_task(
        _bg_full_reset(),
        name=f"astra-reset-remotes-{username}",
    )

    user = await load_user(db, username)
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


def _share_gap_stats(user: User) -> tuple[int, int]:
    """Return (unique_share_count, empty_live_accounts)."""
    from app.adapters.http_utils import is_share_link

    uniq: set[str] = set()
    gaps = 0
    for a in user.panel_accounts or []:
        st = getattr(a.status, "value", a.status)
        links = [
            str(x)
            for x in (a.links or [])
            if x and is_share_link(str(x))
        ]
        for x in links:
            uniq.add(x.split("#", 1)[0])
        if st in ("active", "pending", "error") and not links:
            gaps += 1
    return len(uniq), gaps


@router.post("/api/user/{username}/fix-sub", response_model=UserFixSubResponse)
async def fix_user_subscription(
    username: str,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Heal one user's incomplete subscription feed.

    Retries error/empty accounts, recreates missing group×panel slots, hydrates
    share links, and clears the /sub feed cache — so the next app Update is full.
    """
    from app.adapters.http_utils import is_share_link
    from app.api.subscription import invalidate_user_feed_cache

    user = await load_user(db, username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    before_share, before_gaps = _share_gap_stats(user)
    uid = int(user.id)

    # Empty active rows must be retried like errors (retry only sees error/pending).
    marked = 0
    for a in user.panel_accounts or []:
        st = getattr(a.status, "value", a.status)
        links = [
            str(x)
            for x in (a.links or [])
            if x and is_share_link(str(x))
        ]
        if st in ("active", "pending") and not links:
            a.status = PanelAccountStatus.error
            if not (a.error_message or "").strip():
                a.error_message = "fix-sub: empty links"
            marked += 1
    if marked:
        await db.commit()
        user = await load_user(db, username) or user

    try:
        orchestrator.mark_live_usage_priority(uid)
    except Exception:  # noqa: BLE001
        pass

    retried_n = 0
    try:
        got = await orchestrator.retry_failed_accounts(
            db, user, overall_timeout=45.0
        )
        retried_n = len(got or [])
        await db.commit()
    except Exception:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass

    healed_n = 0
    try:
        healed_n = int(
            await orchestrator.heal_user_coverage_pool_safe(
                uid, wait=28.0, include_empty_links=True
            )
            or 0
        )
    except Exception:  # noqa: BLE001
        healed_n = 0

    hydrated_n = 0
    try:
        hydrated_n = int(
            await orchestrator.hydrate_share_links_pool_safe(uid) or 0
        )
    except Exception:  # noqa: BLE001
        hydrated_n = 0

    try:
        invalidate_user_feed_cache(uid)
    except Exception:  # noqa: BLE001
        pass

    user = await load_user(db, username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    after_share, after_gaps = _share_gap_stats(user)

    if after_gaps <= 0 and after_share >= before_share:
        if after_share > before_share:
            msg = (
                f"لیست تکمیل شد: {before_share} → {after_share} کانفیگ یکتا"
            )
        else:
            msg = f"لیست کامل است ({after_share} کانفیگ یکتا)"
    elif after_share > before_share:
        msg = (
            f"بهبود یافت: {before_share} → {after_share} کانفیگ "
            f"— هنوز {after_gaps} پنل بدون لینک"
        )
    elif after_gaps < before_gaps:
        msg = f"بخشی درست شد — {after_gaps} پنل هنوز لینک ندارد"
    else:
        msg = (
            "تغییر محسوسی نبود؛ پنل‌های قطع/پر ممکن است جلوی ساخت را گرفته باشند"
        )

    return UserFixSubResponse(
        username=user.username,
        before_share=before_share,
        after_share=after_share,
        before_gaps=before_gaps,
        after_gaps=after_gaps,
        retried=retried_n,
        healed=healed_n,
        hydrated=hydrated_n,
        message=msg,
        user=to_user_response(user),
    )


@router.post("/api/users/retry-errors", response_model=UserRetryErrorsResponse)
async def retry_all_errors(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Retry every error/pending remote account in one shot.

    Unreachable panels are probed once and skipped (errors stay). Recovered
    panels clear their user errors. Remaining failures stay on the Users page.
    Also recreates missing group×panel slots lost during panel outages.
    """
    stats = await orchestrator.retry_all_failed_accounts(db)
    try:
        healed = await orchestrator.heal_users_missing_panel_accounts(
            limit_users=40
        )
        if isinstance(stats, dict):
            stats["healed_coverage"] = int(healed or 0)
    except Exception:  # noqa: BLE001
        pass
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        pass
    return UserRetryErrorsResponse(**stats)


def _user_list_scope(
    *,
    search: str | None,
    group_id: int | None,
):
    """Shared WHERE pieces for /api/users list + chip counts."""
    filters: list = []
    if search:
        filters.append(User.username.ilike(f"%{search.strip()}%"))
    needs_group_join = group_id is not None
    group_filter = (
        UserGroup.group_id == int(group_id) if group_id is not None else None
    )
    return filters, needs_group_join, group_filter


def _expired_user_clause(now_ts: int):
    """Match UI: status=expired OR expire timestamp already passed."""
    return or_(
        User.status == UserStatus.expired,
        and_(
            User.expire.is_not(None),
            User.expire > 0,
            User.expire <= int(now_ts),
        ),
    )


def _active_user_clause(now_ts: int):
    """Match UI: status=active and not time-expired."""
    return and_(
        User.status == UserStatus.active,
        or_(
            User.expire.is_(None),
            User.expire <= 0,
            User.expire > int(now_ts),
        ),
    )


def _capacity_error_sql():
    """PasarGuard group cap full — panel online, not a retryable panel fault."""
    msg = PanelAccount.error_message
    return or_(
        msg.ilike("%user limit reached%"),
        msg.ilike("%limit reached%"),
        msg.ilike("%ظرفیت گروه%"),
        msg.ilike("%ظرفیت پر%"),
        msg.ilike("%maximum users%"),
        msg.ilike("%too many users%"),
    )


def _soft_timeout_error_sql():
    """Slow panel create — healable pending noise, not a real fault chip."""
    msg = PanelAccount.error_message
    return or_(
        msg.ilike("%timeout%"),
        msg.ilike("%timed out%"),
        msg.ilike("%waiting for panel response%"),
        msg.ilike("%طول کشید%"),
        msg.ilike("%تایم%"),
    )


def _error_user_clause(now_ts: int | None = None):
    """Active (not expired) user with a real error on a live panel.

    Excludes expired users, sticky rows that already have share links, and
    capacity-full remote groups (panel is up — not an actionable panel fault).
    """
    from sqlalchemy import not_

    ts = int(now_ts if now_ts is not None else time.time())
    return and_(
        _active_user_clause(ts),
        exists(
            select(PanelAccount.id)
            .join(Panel, Panel.id == PanelAccount.panel_id)
            .where(
                PanelAccount.user_id == User.id,
                PanelAccount.status == PanelAccountStatus.error,
                Panel.is_active.is_(True),
                # Sticky «error but already has links» cleared by sweep;
                # still ignore empty-looking payloads when comparable.
                or_(
                    PanelAccount.links.is_(None),
                    PanelAccount.links == [],
                ),
                or_(
                    PanelAccount.error_message.is_(None),
                    PanelAccount.error_message == "",
                    and_(
                        not_(_capacity_error_sql()),
                        not_(_soft_timeout_error_sql()),
                    ),
                ),
            )
        ),
    )


async def _count_users(
    db: AsyncSession,
    *,
    search: str | None,
    group_id: int | None,
    extra=None,
) -> int:
    filters, needs_group_join, group_filter = _user_list_scope(
        search=search, group_id=group_id
    )
    q = select(func.count(func.distinct(User.id)))
    if needs_group_join:
        q = q.join(UserGroup, UserGroup.user_id == User.id)
        if group_filter is not None:
            filters.append(group_filter)
    if filters:
        q = q.where(*filters)
    if extra is not None:
        q = q.where(extra)
    return int((await db.execute(q)).scalar() or 0)


async def _users_list_chip_counts(
    db: AsyncSession,
    *,
    search: str | None,
    group_id: int | None,
    now_ts: int,
) -> UsersListCounts:
    """One round-trip for chip totals — was 4×COUNT and dominated list latency.

    Error chip uses a cheap EXISTS (status=error on active panel) instead of the
    full actionable-error clause with JSON/ILIKE filters — that path scanned too
    hard under bot load and stalled the panel board.
    """
    filters, needs_group_join, group_filter = _user_list_scope(
        search=search, group_id=group_id
    )
    active_c = _active_user_clause(now_ts)
    expired_c = _expired_user_clause(now_ts)
    cheap_error = and_(
        active_c,
        exists(
            select(PanelAccount.id)
            .join(Panel, Panel.id == PanelAccount.panel_id)
            .where(
                PanelAccount.user_id == User.id,
                PanelAccount.status == PanelAccountStatus.error,
                Panel.is_active.is_(True),
            )
        ),
    )
    q = select(
        func.count(func.distinct(User.id)),
        func.count(func.distinct(User.id)).filter(active_c),
        func.count(func.distinct(User.id)).filter(cheap_error),
        func.count(func.distinct(User.id)).filter(expired_c),
    )
    if needs_group_join:
        q = q.join(UserGroup, UserGroup.user_id == User.id)
        if group_filter is not None:
            filters.append(group_filter)
    if filters:
        q = q.where(*filters)
    row = (await db.execute(q)).one()
    return UsersListCounts(
        all=int(row[0] or 0),
        active=int(row[1] or 0),
        error=int(row[2] or 0),
        expired=int(row[3] or 0),
    )


@router.get("/api/users", response_model=UsersListResponse)
async def list_users(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    search: str | None = None,
    group_id: int | None = Query(None, ge=1),
    filter: str | None = Query(
        None,
        pattern="^(all|active|error|expired)$",
        description="Chip filter across all pages: all|active|error|expired",
    ),
    refresh: bool = Query(
        False,
        description="Pull live usage from remote panels for this page (read-only; no remote push)",
    ),
    token: str | None = Depends(oauth2_scheme),
):
    # Acquire the list gate BEFORE opening a DB session — otherwise bot stampedes
    # hold dozens of idle-in-transaction checkouts while queued on the semaphore.
    async with _USERS_LIST_SEM:
        from app.core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            await get_current_admin(request, token, db)
            return await _list_users_impl(
                offset=offset,
                limit=limit,
                search=search,
                group_id=group_id,
                filter=filter,
                refresh=refresh,
                db=db,
            )


async def _list_users_impl(
    *,
    offset: int,
    limit: int,
    search: str | None,
    group_id: int | None,
    filter: str | None,
    refresh: bool,
    db: AsyncSession,
):
    now_ts = int(time.time())
    status_filter = (filter or "all").strip().lower()
    if status_filter not in ("all", "active", "error", "expired"):
        status_filter = "all"
    is_search = bool((search or "").strip())
    # Bot stampede often uses limit=100 search — keep payload tiny.
    if is_search and limit > 40:
        limit = 40

    base_filters, needs_group_join, group_filter = _user_list_scope(
        search=search, group_id=group_id
    )
    if group_filter is not None:
        base_filters = [*base_filters, group_filter]

    expired_c = _expired_user_clause(now_ts)
    active_c = _active_user_clause(now_ts)
    error_c = _error_user_clause(now_ts)

    status_extra = None
    if status_filter == "active":
        status_extra = active_c
    elif status_filter == "error":
        status_extra = error_c
    elif status_filter == "expired":
        status_extra = expired_c

    if is_search:
        # Search: one COUNT + lean rows (no panel_accounts / no error EXISTS).
        total = await _count_users(
            db, search=search, group_id=group_id, extra=status_extra
        )
        # Chips are for the full board; search replies only need matching total.
        counts = UsersListCounts(all=total, active=0, error=0, expired=0)
        q = select(User).options(selectinload(User.groups))
    else:
        # Chip totals — single SQL (keeps filters accurate without 4 round-trips).
        counts = await _users_list_chip_counts(
            db, search=search, group_id=group_id, now_ts=now_ts
        )
        if status_filter == "all":
            total = counts.all
        elif status_filter == "active":
            total = counts.active
        elif status_filter == "error":
            total = counts.error
        elif status_filter == "expired":
            total = counts.expired
        else:
            total = await _count_users(
                db, search=search, group_id=group_id, extra=status_extra
            )
        # Light list load: skip huge share-link JSON + unused panel columns.
        q = select(User).options(
            selectinload(User.groups),
            selectinload(User.panel_accounts).options(
                defer(PanelAccount.links),
                defer(PanelAccount.links_by_group),
                selectinload(PanelAccount.panel).load_only(
                    Panel.id,
                    Panel.name,
                    Panel.type,
                    Panel.is_active,
                    Panel.base_url,
                ),
            ),
        )
    if needs_group_join:
        q = q.join(UserGroup, UserGroup.user_id == User.id)
    if base_filters:
        q = q.where(*base_filters)
    if status_extra is not None:
        q = q.where(status_extra)
    result = await db.execute(
        q.order_by(User.id.desc()).offset(offset).limit(limit)
    )
    users = list(result.scalars().unique().all())
    if not is_search:
        missing = [
            u
            for u in users
            if (u.groups or [])
            and not any(
                a.status != PanelAccountStatus.deleted
                for a in (u.panel_accounts or [])
            )
        ]
        if missing:
            try:
                asyncio.create_task(
                    orchestrator.heal_users_missing_panel_accounts(limit_users=3),
                    name="astra-heal-missing-on-list",
                )
            except Exception:  # noqa: BLE001
                pass

    if refresh and users and not is_search:
        # End list-read txn before parallel remote pulls (idle-in-transaction).
        from app.core.database import release_db_txn

        await release_db_txn(db)
        # Isolated pool-safe pulls — never hold list request txn across remotes.
        await orchestrator.refresh_users_usage_batch(
            db, users, force=True, concurrency=2, push_remotes=False, quick=True
        )
        await db.commit()
        ids = [u.id for u in users]
        result = await db.execute(
            select(User)
            .where(User.id.in_(ids))
            .options(
                selectinload(User.groups),
                selectinload(User.panel_accounts).options(
                    defer(PanelAccount.links),
                    defer(PanelAccount.links_by_group),
                    selectinload(PanelAccount.panel).load_only(
                        Panel.id,
                        Panel.name,
                        Panel.type,
                        Panel.is_active,
                        Panel.base_url,
                    ),
                ),
            )
            .order_by(User.id.desc())
        )
        by_id = {u.id: u for u in result.scalars().unique().all()}
        users = [by_id[i] for i in ids if i in by_id]

    from app.core.database import release_db_txn

    await release_db_txn(db)
    return UsersListResponse(
        users=[to_user_response(u, include_links=False) for u in users],
        total=total,
        counts=counts,
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
    tpl_status = (
        UserStatus(tpl.status)
        if tpl.status in UserStatus.__members__
        else UserStatus.active
    )
    expire = 0
    hold_dur = 0
    if tpl_status == UserStatus.on_hold:
        hold_dur = int(tpl.expire_duration or 0)
    elif tpl.expire_duration:
        expire = int(time.time()) + int(tpl.expire_duration)

    create_body = UserCreate(
        username=username,
        status=tpl_status,
        data_limit=tpl.data_limit,
        expire=expire,
        on_hold_expire_duration=hold_dur,
        note=body.note or tpl.note,
        group_ids=list(tpl.group_ids or []),
        data_limit_reset_strategy=tpl.data_limit_reset_strategy,
        proxy_settings=tpl.extra_settings or {},
    )
    return await create_user(create_body, db, _admin)
