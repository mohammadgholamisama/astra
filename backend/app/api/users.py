import asyncio
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_admin
from app.core.database import get_db
from app.models.group import Group, UserGroup
from app.models.panel import Panel
from app.models.template import UserTemplate
from app.models.user import PanelAccount, PanelAccountStatus, User, UserStatus, generate_sub_token
from app.schemas.user import (
    UserBulkCreate,
    UserBulkCreateResponse,
    UserBulkFail,
    UserCreate,
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

    # Commit user+groups BEFORE panel HTTP so SQLite write lock is not held
    # for the whole create (that was causing "database is locked" / 500).
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

    db.expire_all()
    user = await load_user(db, body.username)
    if not user:
        raise HTTPException(status_code=500, detail="کاربر ساخته شد ولی قابل خواندن نیست")

    try:
        await orchestrator.provision_users_now(db, [user], panels)
    except Exception:  # noqa: BLE001
        log = __import__("logging").getLogger("astra.users")
        log.exception("create provision failed user=%s", body.username)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        try:
            await orchestrator.provision_users_now(db, [user], panels, wait=8.0)
        except Exception:  # noqa: BLE001
            log.exception("create provision retry failed user=%s", body.username)
    try:
        asyncio.create_task(
            orchestrator.heal_users_missing_panel_accounts(limit_users=8),
            name=f"astra-heal-missing-after-create-{body.username}",
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        asyncio.create_task(
            orchestrator.heal_failed_panel_accounts_batch(limit_users=5),
            name=f"astra-heal-after-create-{body.username}",
        )
    except Exception:  # noqa: BLE001
        pass

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
    await orchestrator.refresh_usage(db, user, force=True)
    changed = await orchestrator.enforce_user_quota(db, user)
    # Force push so remotes keep the FULL purchased data_limit (coeff does not shrink),
    # and freeze/disable all panels when Astra weighted usage is exhausted.
    if await orchestrator.should_push_remotes(
        db, user, status_changed=changed, force=True
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

            # 1) Remove remotes for dropped groups BEFORE rewriting membership
            if removed:
                results = await orchestrator.sync_delete_user_group_accounts(
                    db, user, removed
                )
                failed = [r for r in results if not r.get("ok")]
                if failed:
                    await db.commit()  # persist per-panel error_message
                    parts = [
                        f"{r.get('panel_name') or r.get('panel_id')}: {r.get('error') or 'خطا'}"
                        for r in failed
                    ]
                    raise HTTPException(
                        status_code=502,
                        detail="حذف کانفیگ گروه از پنل اصلی ناموفق بود — عضویت گروه عوض نشد: "
                        + "؛ ".join(parts),
                    )

            # 2) Replace membership (add + remove + order) — sub_token unchanged
            await _attach_groups(db, user, wanted)

            # 3) Provision current (panel×group) set — creates missing for added groups
            panels = await orchestrator.resolve_panels_for_groups(db, wanted)
            if not panels:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "NO_ACTIVE_PANELS",
                        "message": "گروه‌های انتخاب‌شده هیچ پنل فعالی ندارند",
                    },
                )
            await orchestrator.provision_user_on_panels(db, user, panels)
            try:
                fresh = await load_user(db, username)
                if fresh:
                    await orchestrator.hydrate_share_links(db, fresh)
                    user = fresh
            except Exception:  # noqa: BLE001
                pass
            _ = added  # reserved for future audit hooks

    # Re-evaluate quota whenever volume / expire / status change.
    # Raising data_limit while status was "limited" must flip back to active
    # BEFORE sync_modify — otherwise remotes stay frozen at used_raw and the
    # new purchase never appears (especially Astra outbound panels).
    if (
        body.data_limit is not None
        or body.expire is not None
        or body.status is not None
    ):
        await orchestrator.enforce_user_quota(db, user)

    # Fast path for volume/expire edits: push Astra node immediately (local DB),
    # then sync foreign remotes in the background so the admin UI isn't blocked.
    from app.models.panel import PanelType

    if groups_changed:
        await orchestrator.sync_modify_user(db, user)
        await db.commit()
    else:
        await orchestrator.sync_modify_user(
            db,
            user,
            only_types={PanelType.astra},
            remote_timeout=20,
            skip_nginx_sync=True,
        )
        await db.commit()
        uid = int(user.id)
        asyncio.create_task(
            orchestrator.sync_modify_user_background(
                uid, exclude_types={PanelType.astra}
            ),
            name=f"astra-sync-remotes-{username}",
        )
    if body.include_promo_first is not None:
        try:
            from app.api.subscription import invalidate_user_feed_cache

            invalidate_user_feed_cache(int(user.id))
        except Exception:  # noqa: BLE001
            pass
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

    # Best-effort remotes: inactive / removed / unreachable panels are skipped
    # so they never block deleting the Astra user.
    results = await orchestrator.sync_delete_user(db, user)
    await db.delete(user)
    await db.commit()
    skipped = [r for r in results if r.get("skipped")]
    return {
        "detail": "User deleted",
        "panels": results,
        "skipped_count": len(skipped),
        "skipped": [
            {
                "panel_id": r.get("panel_id"),
                "panel_name": r.get("panel_name"),
                "reason": r.get("error"),
            }
            for r in skipped
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
    from app.models.panel import PanelType

    # Astra first (local, fast) so UI/volume update immediately; remotes follow.
    results = await orchestrator.reset_user_traffic_on_panels(
        db,
        user,
        prefer_types={PanelType.astra},
        remote_timeout=8,
        sync_after=True,
        sync_only_types={PanelType.astra},
    )
    await db.commit()
    uid = int(user.id)
    asyncio.create_task(
        orchestrator.reset_remotes_background(uid, exclude_types={PanelType.astra}),
        name=f"astra-reset-remotes-{username}",
    )
    user = await load_user(db, username)
    failed = [r for r in results if not r.get("ok")]
    if failed and user:
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


@router.post("/api/users/retry-errors", response_model=UserRetryErrorsResponse)
async def retry_all_errors(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Retry every error/pending remote account in one shot.

    Unreachable panels are probed once and skipped (errors stay). Recovered
    panels clear their user errors. Remaining failures stay on the Users page.
    """
    stats = await orchestrator.retry_all_failed_accounts(db)
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


def _error_user_clause():
    """User has ≥1 error account on a still-active panel (same as UI cards)."""
    return exists(
        select(PanelAccount.id)
        .join(Panel, Panel.id == PanelAccount.panel_id)
        .where(
            PanelAccount.user_id == User.id,
            PanelAccount.status == PanelAccountStatus.error,
            Panel.is_active.is_(True),
        )
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


@router.get("/api/users", response_model=UsersListResponse)
async def list_users(
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
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    now_ts = int(time.time())
    status_filter = (filter or "all").strip().lower()
    if status_filter not in ("all", "active", "error", "expired"):
        status_filter = "all"

    base_filters, needs_group_join, group_filter = _user_list_scope(
        search=search, group_id=group_id
    )
    if group_filter is not None:
        base_filters = [*base_filters, group_filter]

    expired_c = _expired_user_clause(now_ts)
    active_c = _active_user_clause(now_ts)
    error_c = _error_user_clause()

    # Chip totals for current search/group — independent of the selected chip
    counts = UsersListCounts(
        all=await _count_users(db, search=search, group_id=group_id),
        active=await _count_users(
            db, search=search, group_id=group_id, extra=active_c
        ),
        error=await _count_users(
            db, search=search, group_id=group_id, extra=error_c
        ),
        expired=await _count_users(
            db, search=search, group_id=group_id, extra=expired_c
        ),
    )

    status_extra = None
    if status_filter == "active":
        status_extra = active_c
    elif status_filter == "error":
        status_extra = error_c
    elif status_filter == "expired":
        status_extra = expired_c

    total = await _count_users(
        db, search=search, group_id=group_id, extra=status_extra
    )

    q = select(User).options(
        selectinload(User.groups),
        selectinload(User.panel_accounts).selectinload(PanelAccount.panel),
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
                orchestrator.heal_users_missing_panel_accounts(limit_users=12),
                name="astra-heal-missing-on-list",
            )
        except Exception:  # noqa: BLE001
            pass

    if refresh and users:
        # Fail-fast parallel pull for this page only (UI paints DB first, then patches)
        await orchestrator.refresh_users_usage_batch(
            db, users, force=True, concurrency=16, push_remotes=False, quick=True
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
