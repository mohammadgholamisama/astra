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
    UserFixSubBlocker,
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

# Bot/API clients stampede /api/users?search=&limit=100. Keep board (no search)
# on its own semaphore so the Users page is never starved behind bot lookups.
_USERS_BOARD_SEM = asyncio.Semaphore(4)
_USERS_SEARCH_SEM = asyncio.Semaphore(2)


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


# Parallel single-creates (HTTP remotes; DB only short apply sessions).
_CREATE_PROVISION_BG_SEM = asyncio.Semaphore(12)


async def _bg_finish_create_provision(username: str, group_ids: list[int]) -> None:
    """Finish remotes after create returned — fast parallel wave, non-blocking."""
    from app.core.database import AsyncSessionLocal
    from app.models.user import PanelAccount, PanelAccountStatus

    log = __import__("logging").getLogger("astra.users")
    async with _CREATE_PROVISION_BG_SEM:
        try:
            uid = 0
            panels: list = []
            async with AsyncSessionLocal() as db:
                user_row = (
                    await db.execute(select(User).where(User.username == username))
                ).scalar_one_or_none()
                if user_row is None:
                    return
                uid = int(user_row.id)
                raw_panels = await orchestrator.resolve_panels_for_groups(
                    db, group_ids
                )
                # CRITICAL: rollback() expires ORM attrs. Touch+expunge while
                # the session is alive so batch create never hits DetachedInstance.
                panels = [
                    (
                        orchestrator._touch_panel_for_detach(p, db),
                        dict(cfg or {}),
                        int(gid or 0),
                    )
                    for p, cfg, gid in (raw_panels or [])
                ]
                await db.rollback()
            if panels and uid:
                sdb = AsyncSessionLocal()
                try:
                    # Reload user in this session — prior row is detached.
                    user_row = (
                        await sdb.execute(select(User).where(User.id == uid))
                    ).scalar_one_or_none()
                    if user_row is None:
                        return
                    n_jobs = max(1, len(panels))
                    # Enough wall-clock for ≥2 waves of PasarGuard under load.
                    sem_n = min(48, max(24, n_jobs))
                    waves = max(1, (n_jobs + sem_n - 1) // sem_n)
                    create_to = 22.0
                    overall = min(300.0, max(90.0, waves * create_to + 25.0))
                    await orchestrator.provision_users_batch(
                        sdb,
                        [user_row],
                        panels,
                        create_timeout=create_to,
                        concurrency=sem_n,
                        overall_timeout=overall,
                    )
                except Exception:  # noqa: BLE001
                    log.exception(
                        "bg create batch provision failed user=%s", username
                    )
                finally:
                    try:
                        await sdb.close()
                    except Exception:  # noqa: BLE001
                        pass

            if not uid:
                return
            try:
                await orchestrator.hydrate_share_links_pool_safe(uid)
            except Exception:  # noqa: BLE001
                log.debug(
                    "bg create hydrate user=%s failed", username, exc_info=True
                )
            # Drain leftovers with real wait — 3s left 80+ pending after create.
            try:
                pending_n = 0
                async with AsyncSessionLocal() as sdb:
                    pending_n = int(
                        (
                            await sdb.execute(
                                select(func.count())
                                .select_from(PanelAccount)
                                .where(
                                    PanelAccount.user_id == uid,
                                    PanelAccount.status.in_(
                                        (
                                            PanelAccountStatus.pending,
                                            PanelAccountStatus.error,
                                        )
                                    ),
                                )
                            )
                        ).scalar()
                        or 0
                    )
                    await sdb.rollback()
                heal_wait = min(180.0, max(12.0, 2.2 * pending_n + 8.0))
                await orchestrator.heal_user_coverage_pool_safe(
                    uid, wait=heal_wait, include_empty_links=True
                )
            except Exception:  # noqa: BLE001
                log.debug("bg create heal user=%s failed", username, exc_info=True)
            # Brand-new accounts → Hot priority-probe for 15 minutes so
            # used_traffic moves within 3–5s of first connection.
            try:
                from app.services import usage_priority as uprio

                await uprio.enqueue_user_remotes(int(uid), ttl=900.0)
            except Exception:  # noqa: BLE001
                log.debug(
                    "bg create prio-enqueue user=%s failed",
                    username,
                    exc_info=True,
                )
            try:
                pending_ids: list[int] = []
                async with AsyncSessionLocal() as sdb:
                    rows = await sdb.execute(
                        select(PanelAccount.id).where(
                            PanelAccount.user_id == uid,
                            PanelAccount.status.in_(
                                (
                                    PanelAccountStatus.pending,
                                    PanelAccountStatus.error,
                                )
                            ),
                        )
                    )
                    pending_ids = [int(r[0]) for r in rows.all() if r[0]]
                    await sdb.rollback()
                if pending_ids:
                    orchestrator.start_account_provisions(pending_ids)
            except Exception:  # noqa: BLE001
                log.debug(
                    "bg create leftover enqueue user=%s failed",
                    username,
                    exc_info=True,
                )
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

    # From here the user EXISTS — seed pending PanelAccount rows so UI never
    # shows ۰/۰, kick durable drains, then finish remotes in background.
    try:
        await orchestrator.ensure_pending_panel_accounts(db, [user], panels)
        await db.commit()
    except Exception:  # noqa: BLE001
        log.exception("create ensure_pending failed user=%s", body.username)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass

    try:
        pending_ids = [
            int(a.id)
            for a in (
                (
                    await db.execute(
                        select(PanelAccount.id).where(
                            PanelAccount.user_id == int(user.id),
                            PanelAccount.status.in_(
                                (
                                    PanelAccountStatus.pending,
                                    PanelAccountStatus.error,
                                )
                            ),
                        )
                    )
                ).all()
            )
            if a[0]
        ]
        if pending_ids:
            orchestrator.start_account_provisions(pending_ids)
    except Exception:  # noqa: BLE001
        log.debug("create kick pending failed user=%s", body.username, exc_info=True)

    try:
        asyncio.create_task(
            _bg_finish_create_provision(body.username, list(body.group_ids)),
            name=f"astra-create-finish-{body.username}",
        )
    except Exception:  # noqa: BLE001
        pass

    user_out = await load_user(db, body.username)
    if user_out is not None:
        return to_user_response(user_out, include_links=False)

    from app.services.users import subscription_url

    return UserResponse(
        username=str(user.username),
        display_name=(getattr(user, "display_name", None) or None),
        status=user.status,
        used_traffic=0,
        lifetime_used_traffic=0,
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

    from app.core.database import release_db_txn
    from app.services.users import subscription_url

    created_names = [u.username for u in users]
    user_ids = [int(u.id) for u in users if getattr(u, "id", None)]

    # Pending PanelAccount rows BEFORE HTTP return — cards never stick at ۰/۰.
    # Remote create still runs in background; UI shows N/N pending immediately.
    if user_ids and panel_id_set:
        try:
            rows = await db.execute(
                select(Panel).where(Panel.id.in_(panel_id_set or [-1]))
            )
            by_id = {int(p.id): p for p in rows.scalars().all()}
            local_panels = [
                (by_id[pid], cfg, gid)
                for pid, cfg, gid in panel_specs
                if pid in by_id and bool(getattr(by_id[pid], "is_active", True))
            ]
            if local_panels:
                await orchestrator.ensure_pending_panel_accounts(
                    db, users, local_panels
                )
                await db.commit()
                # Durable kick: even if the bulk bg task dies on worker restart,
                # pending rows + start_account_provisions / drain loop finish remotes.
                try:
                    rows_p = await db.execute(
                        select(PanelAccount.id).where(
                            PanelAccount.user_id.in_(user_ids),
                            PanelAccount.status.in_(
                                (
                                    PanelAccountStatus.pending,
                                    PanelAccountStatus.error,
                                )
                            ),
                        )
                    )
                    kick_ids = [int(r[0]) for r in rows_p.all() if r[0]]
                    if kick_ids:
                        orchestrator.start_account_provisions(kick_ids)
                except Exception:  # noqa: BLE001
                    log.debug("bulk kick pending soft-fail", exc_info=True)
        except Exception:  # noqa: BLE001
            log.exception("bulk ensure_pending failed — remotes still scheduled")
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass

    # Serialize with pending accounts when present (sub URL ready; remotes bg).
    created: list[UserResponse] = []
    try:
        loaded = await db.execute(
            select(User)
            .where(User.id.in_(user_ids or [-1]))
            .options(
                selectinload(User.groups).load_only(Group.id),
                selectinload(User.panel_accounts).options(
                    selectinload(PanelAccount.panel).load_only(
                        Panel.id,
                        Panel.name,
                        Panel.type,
                        Panel.is_active,
                        Panel.base_url,
                    ),
                ),
            )
        )
        by_id = {int(u.id): u for u in loaded.scalars().unique().all()}
        for user in users:
            u2 = by_id.get(int(user.id)) if getattr(user, "id", None) else None
            try:
                if u2 is not None:
                    created.append(to_user_response(u2, include_links=False))
                else:
                    created.append(
                        UserResponse(
                            username=user.username,
                            display_name=user.display_name,
                            status=user.status,
                            used_traffic=int(user.used_traffic or 0),
                            lifetime_used_traffic=int(
                                user.lifetime_used_traffic or 0
                            ),
                            data_limit=int(user.data_limit or 0),
                            expire=int(user.expire or 0),
                            on_hold_expire_duration=int(
                                getattr(user, "on_hold_expire_duration", 0) or 0
                            ),
                            include_promo_first=bool(
                                getattr(user, "include_promo_first", True)
                            ),
                            note=user.note,
                            created_at=getattr(user, "created_at", None),
                            subscription_url=subscription_url(user),
                            group_ids=list(ordered_gids),
                            source_category_id=getattr(
                                user, "source_category_id", None
                            ),
                            panel_accounts=[],
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("bulk serialize user=%s: %s", user.username, exc)
                failed.append(
                    UserBulkFail(
                        username=user.username,
                        message="کاربر ساخته شد ولی لینک قابل ساخت نیست",
                    )
                )
    except Exception:  # noqa: BLE001
        log.exception("bulk serialize reload failed")
        for user in users:
            try:
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
                        created_at=getattr(user, "created_at", None),
                        subscription_url=subscription_url(user),
                        group_ids=list(ordered_gids),
                        source_category_id=getattr(user, "source_category_id", None),
                        panel_accounts=[],
                    )
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("bulk serialize fallback user=%s: %s", user.username, exc)
                failed.append(
                    UserBulkFail(
                        username=user.username,
                        message="کاربر ساخته شد ولی لینک قابل ساخت نیست",
                    )
                )

    await release_db_txn(db)

    if user_ids and panel_id_set:
        specs = list(panel_specs)
        uids = list(user_ids)

        async def _bulk_provision_bg() -> None:
            from app.core.database import AsyncSessionLocal
            from app.api.subscription import invalidate_user_feed_cache

            try:
                # Load targets, then CLOSE the session before the remote wave
                # so one bulk create cannot pin a pool checkout for minutes.
                async with AsyncSessionLocal() as sdb:
                    loaded = await sdb.execute(
                        select(User).where(User.id.in_(uids))
                    )
                    bg_users = list(loaded.scalars().all())
                    if not bg_users:
                        return
                    rows = await sdb.execute(
                        select(Panel).where(Panel.id.in_(panel_id_set or [-1]))
                    )
                    by_id = {
                        int(p.id): orchestrator._touch_panel_for_detach(p, sdb)
                        for p in rows.scalars().all()
                    }
                    local_panels = [
                        (by_id[pid], cfg, gid)
                        for pid, cfg, gid in specs
                        if pid in by_id
                        and bool(getattr(by_id[pid], "is_active", True))
                    ]
                    # Touch users before rollback so batch prepare never Detaches.
                    for u in bg_users:
                        _ = (u.id, u.username, u.status, u.data_limit, u.expire)
                        try:
                            sdb.expunge(u)
                        except Exception:  # noqa: BLE001
                            pass
                    await sdb.rollback()
                if not local_panels:
                    return
                n_jobs = max(1, len(bg_users) * max(1, len(local_panels)))
                # Cap concurrency under DB pool — stampede left cards at ۰/N.
                # provision_users_batch closes the session after prepare.
                sdb = AsyncSessionLocal()
                try:
                    # Fresh user rows in this session (expunged snaps lack relations).
                    loaded2 = await sdb.execute(
                        select(User).where(User.id.in_(uids))
                    )
                    bg_users2 = list(loaded2.scalars().all())
                    create_to = 18.0
                    sem_n = min(40, max(16, min(n_jobs, 40)))
                    waves = max(1, (n_jobs + sem_n - 1) // sem_n)
                    overall = min(480.0, max(90.0, waves * create_to + 30.0))
                    await orchestrator.provision_users_batch(
                        sdb,
                        bg_users2 or bg_users,
                        local_panels,
                        create_timeout=create_to,
                        concurrency=sem_n,
                        overall_timeout=overall,
                    )
                finally:
                    try:
                        await sdb.close()
                    except Exception:  # noqa: BLE001
                        pass

                # Fill share links so /sub is not empty after remotes land.
                for uid in uids:
                    try:
                        await orchestrator.hydrate_share_links_pool_safe(int(uid))
                    except Exception:  # noqa: BLE001
                        log.debug(
                            "bulk hydrate soft-fail user_id=%s", uid, exc_info=True
                        )

                # Keep draining any leftover pending/error accounts.
                try:
                    await orchestrator.heal_failed_panel_accounts_batch(
                        limit_users=max(20, len(uids) * 3)
                    )
                except Exception:  # noqa: BLE001
                    log.debug("bulk bg heal soft-fail", exc_info=True)
                try:
                    pending_ids: list[int] = []
                    async with AsyncSessionLocal() as sdb:
                        rows = await sdb.execute(
                            select(PanelAccount.id).where(
                                PanelAccount.user_id.in_(uids),
                                PanelAccount.status.in_(
                                    (
                                        PanelAccountStatus.pending,
                                        PanelAccountStatus.error,
                                    )
                                ),
                            )
                        )
                        pending_ids = [int(r[0]) for r in rows.all() if r[0]]
                        await sdb.rollback()
                    if pending_ids:
                        orchestrator.start_account_provisions(pending_ids)
                except Exception:  # noqa: BLE001
                    log.debug("bulk leftover enqueue soft-fail", exc_info=True)

                # Final safety: anyone still with zero active remotes gets a
                # dedicated provision pass so bulk never leaves empty subs.
                try:
                    empty_ids: list[int] = []
                    async with AsyncSessionLocal() as sdb:
                        for uid in uids:
                            n_active = (
                                await sdb.execute(
                                    select(func.count())
                                    .select_from(PanelAccount)
                                    .where(
                                        PanelAccount.user_id == int(uid),
                                        PanelAccount.status
                                        == PanelAccountStatus.active,
                                    )
                                )
                            ).scalar() or 0
                            if int(n_active) <= 0:
                                empty_ids.append(int(uid))
                        await sdb.rollback()
                    for uid in empty_ids:
                        try:
                            async with AsyncSessionLocal() as sdb:
                                urow = await sdb.execute(
                                    select(User).where(User.id == uid)
                                )
                                u = urow.scalar_one_or_none()
                                if u is None:
                                    continue
                                ug = await sdb.execute(
                                    select(UserGroup.group_id).where(
                                        UserGroup.user_id == uid
                                    )
                                )
                                gids = [
                                    int(r[0])
                                    for r in ug.all()
                                    if r[0] is not None
                                ]
                                targets = (
                                    await orchestrator.resolve_panels_for_groups(
                                        sdb, gids
                                    )
                                    if gids
                                    else []
                                )
                                if not targets:
                                    continue
                                await orchestrator.provision_user_on_panels(
                                    sdb,
                                    u,
                                    targets,
                                    fast=True,
                                    overall_timeout=min(
                                        120.0,
                                        max(40.0, 8.0 + 2.5 * len(targets)),
                                    ),
                                    skip_astra_rebuild=True,
                                )
                                await sdb.commit()
                            await orchestrator.hydrate_share_links_pool_safe(
                                int(uid)
                            )
                        except Exception:  # noqa: BLE001
                            log.exception(
                                "bulk empty-user retry failed user_id=%s", uid
                            )
                except Exception:  # noqa: BLE001
                    log.exception("bulk empty-user sweep failed")

                try:
                    invalidate_user_feed_cache(*uids)
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                log.exception("bulk background provision failed")

        try:
            asyncio.create_task(
                _bulk_provision_bg(),
                name="astra-bulk-provision-bg",
            )
        except Exception:  # noqa: BLE001
            log.exception("bulk bg task schedule failed")

    return UserBulkCreateResponse(
        created=created,
        failed=failed,
        total_requested=len(names),
        remote_bg=bool(user_ids and panel_id_set),
        message=(
            "کاربران و لینک ساب ساخته شدند — ساخت پنل‌ها و کانفیگ در پس‌زمینه با سرعت بالا ادامه دارد"
            if user_ids and panel_id_set
            else ""
        ),
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
            if incoming == UserStatus.active:
                try:
                    from app.services.worker_commands import clear_kill_cooldown

                    clear_kill_cooldown(int(user.id))
                except Exception:  # noqa: BLE001
                    pass
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

    # Admin «چک مصرف»: if users.used_traffic is inflated vs live panel meters
    # (e.g. 2× double-count), shrink to truth and re-enforce quota. Normal
    # refresh is monotonic on purpose — this path must correct false اتمام حجم.
    shrunk = False
    try:
        fixed = await orchestrator.reconcile_user_usage_if_inflated(uid)
        if fixed is not None:
            after = int(fixed)
            shrunk = True
    except Exception:  # noqa: BLE001
        pass

    user = await load_user(db, username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    used_after = int(user.used_traffic or 0)
    if after >= 0 and not shrunk:
        used_after = max(used_after, int(after))
    elif after >= 0 and shrunk:
        used_after = int(after)

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
    if shrunk and delta < 0:
        msg = (
            f"مصرف اصلاح شد (تورم متر): {used_before} → {used_after} بایت "
            f"({synced}/{len(panels_out)} پنل)"
        )
    elif delta > 0:
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
        try:
            from app.services.worker_commands import clear_kill_cooldown

            clear_kill_cooldown(user_id)
        except Exception:  # noqa: BLE001
            pass
    acc_result = await db.execute(
        select(PanelAccount).where(PanelAccount.user_id == user_id)
    )
    panel_ids_for_circuit: list[int] = []
    for acc in acc_result.scalars().all():
        if acc.status == PanelAccountStatus.deleted:
            continue
        acc.used_traffic = 0
        acc.last_remote_raw = 0
        acc.coeff_anchor_raw = 0
        acc.coeff_anchor_weighted = 0
        # Unfreeze locally so /sub + heal can restore the SAME remotes/links.
        acc.status = PanelAccountStatus.active
        acc.error_message = None
        try:
            panel_ids_for_circuit.append(int(acc.panel_id))
        except Exception:  # noqa: BLE001
            pass
    await db.commit()
    try:
        from app.api.subscription import invalidate_user_feed_cache

        invalidate_user_feed_cache(user_id)
    except Exception:  # noqa: BLE001
        pass
    # Reset stampede must not keep circuits open — otherwise hydrate skips
    # every panel and the subscription stays empty.
    try:
        from app.core.panel_circuit import clear_panel_circuit

        for pid in dict.fromkeys(panel_ids_for_circuit):
            clear_panel_circuit(int(pid))
    except Exception:  # noqa: BLE001
        pass

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
                    remote_timeout=14,
                    sync_after=True,
                    sync_remote_timeout=16,
                    concurrency=12,
                )
                u.used_traffic = 0
                if u.status == UserStatus.limited:
                    u.status = UserStatus.active
                # Keep local meters/cursors clean after remote echo.
                for acc in list(getattr(u, "panel_accounts", None) or []):
                    if acc.status == PanelAccountStatus.deleted:
                        continue
                    acc.used_traffic = 0
                    acc.last_remote_raw = 0
                    acc.coeff_anchor_raw = 0
                    acc.coeff_anchor_weighted = 0
                    if acc.status != PanelAccountStatus.active:
                        acc.status = PanelAccountStatus.active
                    if (acc.error_message or "").startswith("ASTRA_QUOTA_FROZEN"):
                        acc.error_message = None
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
                        try:
                            from app.core.panel_circuit import clear_panel_circuit

                            for pid in fail_ids:
                                clear_panel_circuit(int(pid))
                        except Exception:  # noqa: BLE001
                            pass
                        async with AsyncSessionLocal() as rdb:
                            uu = await load_user(rdb, username)
                            if not uu:
                                return
                            await orchestrator.reset_user_traffic_on_panels(
                                rdb,
                                uu,
                                panel_ids=fail_ids,
                                remote_timeout=16,
                                sync_after=True,
                                sync_remote_timeout=18,
                                concurrency=8,
                            )
                            uu.used_traffic = 0
                            if uu.status == UserStatus.limited:
                                uu.status = UserStatus.active
                            await rdb.commit()
            # Restore share links so /sub updates on the same configs.
            try:
                await orchestrator.hydrate_share_links_pool_safe(user_id)
            except Exception:  # noqa: BLE001
                logger.exception("reset hydrate failed user=%s", username)
            try:
                from app.api.subscription import invalidate_user_feed_cache

                invalidate_user_feed_cache(user_id)
            except Exception:  # noqa: BLE001
                pass
            # Coverage heal if any slot still has empty links after reset.
            try:
                await orchestrator.heal_user_coverage_pool_safe(user_id)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "reset coverage heal soft-fail user=%s", username, exc_info=True
                )
            try:
                from app.api.subscription import invalidate_user_feed_cache

                invalidate_user_feed_cache(user_id)
            except Exception:  # noqa: BLE001
                pass
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


def _looks_capacity_err(msg: str | None) -> bool:
    m = (msg or "").lower()
    if not m:
        return False
    if "user limit reached" in m or "limit reached" in m:
        return True
    if "maximum users" in m or "too many users" in m:
        return True
    if "ظرفیت" in (msg or "") and ("پر" in (msg or "") or "گروه" in (msg or "")):
        return True
    return False


def _looks_timeout_err(msg: str | None) -> bool:
    m = (msg or "").lower()
    return bool(
        m
        and (
            "timeout" in m
            or "timed out" in m
            or "waiting for panel response" in m
            or "طول کشید" in (msg or "")
        )
    )


async def _stamp_fix_sub_blockers(db: AsyncSession, user: User) -> list[dict]:
    """Mark remaining empty-link accounts with a concrete reason for the errors UI.

    Silent ``active`` + empty links hid Out122-class failures from «خطاها».
    """
    from app.adapters.http_utils import is_share_link
    from app.models.panel import PanelType
    from app.schemas.user import UserFixSubBlocker
    from app.services import astra_node as astra_node_svc

    blockers: list[dict] = []
    touched = 0
    # Cache outbound checks per panel
    outbound_cache: dict[int, tuple[str, str]] = {}

    for a in user.panel_accounts or []:
        st = getattr(a.status, "value", a.status)
        if st not in ("active", "pending", "error"):
            continue
        if getattr(a, "panel", None) is not None and not getattr(
            a.panel, "is_active", True
        ):
            continue
        links = [
            str(x)
            for x in (a.links or [])
            if x and is_share_link(str(x))
        ]
        if links:
            continue

        panel = getattr(a, "panel", None)
        pname = (panel.name if panel else None) or f"پنل #{a.panel_id}"
        prev = (a.error_message or "").strip()
        kind = "empty"
        reason = prev

        if _looks_capacity_err(prev):
            kind = "capacity"
            reason = prev
        elif _looks_timeout_err(prev):
            kind = "timeout"
            reason = prev
        elif panel is not None and panel.type == PanelType.astra:
            pid = int(panel.id)
            if pid not in outbound_cache:
                try:
                    outs = await astra_node_svc.list_outbounds(db, pid)
                except Exception:  # noqa: BLE001
                    outs = []
                enabled = [o for o in outs if getattr(o, "is_enabled", False)]
                disabled = [o for o in outs if not getattr(o, "is_enabled", False)]
                dead_enabled = [
                    o
                    for o in enabled
                    if getattr(o, "last_ping_ok", None) is False
                ]
                if not outs:
                    outbound_cache[pid] = (
                        "no_outbound",
                        f"اوتباندی روی پنل «{pname}» تعریف نشده — لینک ساخته نمی‌شود",
                    )
                elif not enabled:
                    labels = [
                        astra_node_svc.outbound_display_name(o) for o in disabled
                    ]
                    named = astra_node_svc._format_outbound_names(labels)
                    host = getattr(disabled[0], "host", None) or "?"
                    port = getattr(disabled[0], "port", None) or "?"
                    outbound_cache[pid] = (
                        "outbound_disabled",
                        (
                            f"اوتباند {named} خاموش است"
                            f" ({host}:{port}) — لینک ساخته نمی‌شود"
                            if named
                            else f"اوتباند پنل «{pname}» خاموش است — لینک ساخته نمی‌شود"
                        ),
                    )
                elif dead_enabled and len(dead_enabled) == len(enabled):
                    labels = [
                        astra_node_svc.outbound_display_name(o) for o in dead_enabled
                    ]
                    named = astra_node_svc._format_outbound_names(labels)
                    outbound_cache[pid] = (
                        "outbound_disabled",
                        (
                            f"اوتباند {named} قطع است (پینگ ناموفق) — لینک ساخته نمی‌شود"
                            if named
                            else f"اوتباند پنل «{pname}» قطع است — لینک ساخته نمی‌شود"
                        ),
                    )
                else:
                    outbound_cache[pid] = (
                        "empty",
                        f"پنل «{pname}»: کلاینت هست ولی لینک share خالی ماند",
                    )
            kind, reason = outbound_cache[pid]
        elif prev:
            kind = "error"
            reason = prev
        else:
            reason = f"پنل {pname}: بدون لینک — ساخت/hydrate ناموفق بود"

        if a.status != PanelAccountStatus.error or (a.error_message or "") != reason:
            a.status = PanelAccountStatus.error
            a.error_message = reason
            touched += 1

        blockers.append(
            UserFixSubBlocker(
                panel_id=int(a.panel_id),
                panel_name=pname,
                kind=kind,
                reason=reason,
            ).model_dump()
        )

    if touched:
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
    return blockers


@router.post("/api/user/{username}/fix-sub", response_model=UserFixSubResponse)
async def fix_user_subscription(
    username: str,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Heal one user's incomplete subscription feed.

    Retries error/empty accounts, recreates missing group×panel slots, hydrates
    share links, and clears the /sub feed cache — so the next app Update is full.
    Remaining gaps are stamped with a concrete panel reason for «خطاها».
    """
    from app.adapters.http_utils import is_share_link
    from app.api.subscription import invalidate_user_feed_cache
    from app.schemas.user import UserFixSubBlocker

    user = await load_user(db, username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    before_share, before_gaps = _share_gap_stats(user)
    uid = int(user.id)

    # Empty /sub-only / generic stamp → pending so create+expand run for real.
    marked = 0
    for a in user.panel_accounts or []:
        st = getattr(a.status, "value", a.status)
        links = [
            str(x)
            for x in (a.links or [])
            if x and is_share_link(str(x))
        ]
        err = (a.error_message or "").strip()
        generic = (
            not err
            or err.startswith("fix-sub:")
            or "بدون لینک" in err
            or "ساخت/hydrate" in err
        )
        if st in ("active", "pending", "error") and not links:
            a.status = PanelAccountStatus.pending if generic else PanelAccountStatus.error
            if generic:
                a.error_message = None
            elif not err:
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
            db, user, overall_timeout=75.0
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
                uid, wait=55.0, include_empty_links=True
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
    # Second expand pass — PasarGuard often lands /sub first, shares on hydrate#2.
    try:
        hydrated_n += int(
            await orchestrator.hydrate_share_links_pool_safe(uid) or 0
        )
    except Exception:  # noqa: BLE001
        pass

    try:
        invalidate_user_feed_cache(uid)
    except Exception:  # noqa: BLE001
        pass

    user = await load_user(db, username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    after_share, after_gaps = _share_gap_stats(user)

    blockers_raw = await _stamp_fix_sub_blockers(db, user)
    # Reload so response carries stamped errors into the UI errors modal.
    user = await load_user(db, username) or user
    after_share, after_gaps = _share_gap_stats(user)
    blockers = [UserFixSubBlocker(**b) for b in blockers_raw]

    if after_gaps <= 0 and after_share >= before_share:
        if after_share > before_share:
            msg = (
                f"لیست تکمیل شد: {before_share} → {after_share} کانفیگ یکتا"
            )
        else:
            msg = f"لیست کامل است ({after_share} کانفیگ یکتا)"
    elif blockers:
        # Name the first few panels so admin sees the real cause immediately.
        parts: list[str] = []
        for b in blockers[:4]:
            label = b.panel_name or f"#{b.panel_id}"
            if b.kind == "capacity":
                parts.append(f"{label} (ظرفیت پر)")
            elif b.kind == "outbound_disabled":
                parts.append(f"{label} (اوتباند خاموش)")
            elif b.kind == "no_outbound":
                parts.append(f"{label} (بدون اوتباند)")
            elif b.kind == "timeout":
                parts.append(f"{label} (تایم‌اوت)")
            else:
                parts.append(label)
        extra = f" و {len(blockers) - 4} مورد دیگر" if len(blockers) > 4 else ""
        if after_share > before_share:
            msg = (
                f"بهبود {before_share}→{after_share}؛ هنوز گیر: "
                + "، ".join(parts)
                + extra
            )
        else:
            msg = "تکمیل نشد — " + "، ".join(parts) + extra
    elif after_share > before_share:
        msg = (
            f"بهبود یافت: {before_share} → {after_share} کانفیگ "
            f"— هنوز {after_gaps} پنل بدون لینک"
        )
    elif after_gaps < before_gaps:
        msg = f"بخشی درست شد — {after_gaps} پنل هنوز لینک ندارد"
    else:
        msg = (
            "تغییر محسوسی نبود؛ جزئیات را در خطاهای همین کاربر ببینید"
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
        blockers=blockers,
        user=to_user_response(user),
    )


@router.post("/api/users/retry-errors", response_model=UserRetryErrorsResponse)
async def retry_all_errors(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Start a fleet-wide error heal — never await remotes on the HTTP path.

    Waiting on thousands of panel creates caused Cloudflare/browser
    ``Failed to fetch``. Instant local sweep + background waves instead.
    """
    import logging

    from app.core.database import AsyncSessionLocal, release_db_txn
    from sqlalchemy import text

    log = logging.getLogger("astra.users")

    # 1) Instant local cleanup (no remote HTTP) — shrinks the scary banner.
    sweep = await orchestrator.sweep_stale_panel_errors(db)
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass

    # Remaining actionable error/pending rows (for UI feedback).
    try:
        remaining = int(
            (
                await db.execute(
                    text(
                        """
                        SELECT count(*) FROM panel_accounts pa
                        JOIN panels p ON p.id = pa.panel_id
                        JOIN users u ON u.id = pa.user_id
                        WHERE pa.status IN ('error', 'pending')
                          AND p.is_active IS TRUE
                          AND u.status = 'active'
                          AND COALESCE(pa.error_message, '') NOT ILIKE '%user limit reached%'
                          AND COALESCE(pa.error_message, '') NOT ILIKE '%limit reached%'
                          AND COALESCE(pa.error_message, '') NOT ILIKE '%ظرفیت%'
                        """
                    )
                )
            ).scalar()
            or 0
        )
    except Exception:  # noqa: BLE001
        remaining = 0

    await release_db_txn(db)

    # 2) Heavy remote work in background — batch waves under the bulk lock.
    async def _bg_retry_waves() -> None:
        try:
            async with AsyncSessionLocal() as sdb:
                # First wave: fair batch so healthy panels recover fast.
                await orchestrator.retry_all_failed_accounts(
                    sdb,
                    overall_timeout=240.0,
                    per_remote_timeout=18.0,
                    probe_timeout=6.0,
                    concurrency=28,
                    limit_accounts=800,
                )
                try:
                    await sdb.commit()
                except Exception:  # noqa: BLE001
                    try:
                        await sdb.rollback()
                    except Exception:  # noqa: BLE001
                        pass
            # Coverage heal for users missing group×panel slots.
            try:
                await orchestrator.heal_users_missing_panel_accounts(
                    limit_users=80
                )
            except Exception:  # noqa: BLE001
                log.debug("retry-errors bg coverage soft-fail", exc_info=True)
            # Drain leftover pending via high-concurrency provision queue.
            try:
                async with AsyncSessionLocal() as sdb:
                    rows = await sdb.execute(
                        select(PanelAccount.id)
                        .where(
                            PanelAccount.status.in_(
                                (
                                    PanelAccountStatus.pending,
                                    PanelAccountStatus.error,
                                )
                            )
                        )
                        .order_by(PanelAccount.id.asc())
                        .limit(4000)
                    )
                    ids = [int(r[0]) for r in rows.all() if r[0]]
                    await sdb.rollback()
                if ids:
                    orchestrator.start_account_provisions(ids)
            except Exception:  # noqa: BLE001
                log.debug("retry-errors bg enqueue soft-fail", exc_info=True)
            # Second wave if backlog remains.
            try:
                async with AsyncSessionLocal() as sdb:
                    await orchestrator.retry_all_failed_accounts(
                        sdb,
                        overall_timeout=300.0,
                        per_remote_timeout=20.0,
                        probe_timeout=6.0,
                        concurrency=24,
                        limit_accounts=1200,
                    )
                    try:
                        await sdb.commit()
                    except Exception:  # noqa: BLE001
                        try:
                            await sdb.rollback()
                        except Exception:  # noqa: BLE001
                            pass
            except Exception:  # noqa: BLE001
                log.debug("retry-errors second wave soft-fail", exc_info=True)
        except Exception:  # noqa: BLE001
            log.exception("retry-errors background failed")

    already = orchestrator.bulk_retry_in_progress()
    if not already:
        try:
            asyncio.create_task(
                _bg_retry_waves(), name="astra-retry-errors-bg"
            )
        except Exception:  # noqa: BLE001
            log.exception("retry-errors schedule failed")

    swept = (
        int(sweep.get("swept_active") or 0)
        + int(sweep.get("swept_pending") or 0)
        + int(sweep.get("swept_inactive") or 0)
    )
    msg = (
        "پاک‌سازی فوری انجام شد — ساخت/رفع روی پنل‌ها در پس‌زمینه با سرعت بالا ادامه دارد"
        if not already
        else "یک تلاش مجدد از قبل در جریان است — همان موج ادامه دارد"
    )
    return UserRetryErrorsResponse(
        attempted=0,
        recovered=int(sweep.get("swept_active") or 0),
        remaining=int(remaining),
        dropped=int(sweep.get("swept_inactive") or 0),
        swept_active=int(sweep.get("swept_active") or 0),
        swept_pending=int(sweep.get("swept_pending") or 0),
        swept_inactive=int(sweep.get("swept_inactive") or 0),
        remote_bg=True,
        message=msg if (remaining > 0 or swept > 0 or already) else "خطای فعالی برای تلاش مجدد نبود",
    )


def _like_escape(term: str) -> str:
    """Escape ``%`` / ``_`` / ``\\`` so user input is literal in ILIKE."""
    return (
        (term or "")
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _username_search_filter(search: str):
    """Typeahead search: username prefix + display_name contains.

    Previously any clean 2+ char term forced *exact* username equality, so
    typing ``Fa`` → ``Fas`` → ``Fast`` emptied the list until the full name
    was entered. Prefix match restores progressive search and stays index-friendly.
    """
    term = (search or "").strip()
    if not term:
        return User.id.is_not(None)
    esc = _like_escape(term)
    clauses = [User.username.ilike(f"{esc}%", escape="\\")]
    # Also match display name once the query is meaningful.
    if len(term) >= 2:
        clauses.append(User.display_name.ilike(f"%{esc}%", escape="\\"))
    return or_(*clauses)


def _user_list_scope(
    *,
    search: str | None,
    group_id: int | None,
):
    """Shared WHERE pieces for /api/users list + chip counts."""
    filters: list = []
    if search and search.strip():
        filters.append(_username_search_filter(search))
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


def _limited_user_clause(now_ts: int):
    """Match UI chip «اتمام حجم»: status=limited and not time-expired."""
    return and_(
        User.status == UserStatus.limited,
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


def _db_busy_error_sql():
    """Postgres/pool saturation on master or node — transient, not a panel fault."""
    msg = PanelAccount.error_message
    return or_(
        msg.ilike("%too many clients%"),
        msg.ilike("%remaining connection slots%"),
        msg.ilike("%queuepool%"),
        msg.ilike("%Too many open files%"),
        msg.ilike("%connection is closed%"),
        msg.ilike("%PendingRollback%"),
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
                        not_(_db_busy_error_sql()),
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
    """Chip totals for the board.

    Default (no search/group): indexed aggregates — error chip is
    ``COUNT(DISTINCT user_id)`` from ``panel_accounts`` (status index) joined to
    active users, not a correlated EXISTS over every user row.
    """
    from sqlalchemy import text

    filters, needs_group_join, group_filter = _user_list_scope(
        search=search, group_id=group_id
    )
    # Fast default board path.
    if not filters and not needs_group_join:
        row = (
            await db.execute(
                text(
                    """
                    SELECT
                      (SELECT count(*) FROM users) AS all_c,
                      (SELECT count(*) FROM users
                         WHERE status = 'active'
                           AND (expire IS NULL OR expire <= 0 OR expire > :now_ts)
                      ) AS active_c,
                      (SELECT count(*) FROM users
                         WHERE status = 'expired'
                            OR (expire IS NOT NULL AND expire > 0 AND expire <= :now_ts)
                      ) AS expired_c,
                      (SELECT count(*) FROM users
                         WHERE status = 'limited'
                           AND (expire IS NULL OR expire <= 0 OR expire > :now_ts)
                      ) AS limited_c,
                      (SELECT count(DISTINCT pa.user_id)
                         FROM panel_accounts pa
                         JOIN panels p ON p.id = pa.panel_id
                         JOIN users u ON u.id = pa.user_id
                        WHERE pa.status = 'error'
                          AND p.is_active IS TRUE
                          AND u.status = 'active'
                          AND (u.expire IS NULL OR u.expire <= 0 OR u.expire > :now_ts)
                          AND (pa.links IS NULL OR pa.links = '[]'::jsonb)
                          AND COALESCE(pa.error_message, '') <> 'ASTRA_QUOTA_FROZEN'
                          AND COALESCE(pa.error_message, '') NOT ILIKE '%user limit reached%'
                          AND COALESCE(pa.error_message, '') NOT ILIKE '%limit reached%'
                          AND COALESCE(pa.error_message, '') NOT ILIKE '%ظرفیت%'
                          AND COALESCE(pa.error_message, '') NOT ILIKE '%maximum users%'
                          AND COALESCE(pa.error_message, '') NOT ILIKE '%too many users%'
                          AND COALESCE(pa.error_message, '') NOT ILIKE '%timeout%'
                          AND COALESCE(pa.error_message, '') NOT ILIKE '%timed out%'
                          AND COALESCE(pa.error_message, '') NOT ILIKE '%waiting for panel response%'
                          AND COALESCE(pa.error_message, '') NOT ILIKE '%طول کشید%'
                          AND COALESCE(pa.error_message, '') NOT ILIKE '%تایم%'
                          AND COALESCE(pa.error_message, '') NOT ILIKE '%too many clients%'
                          AND COALESCE(pa.error_message, '') NOT ILIKE '%queuepool%'
                          AND COALESCE(pa.error_message, '') NOT ILIKE '%PendingRollback%'
                      ) AS error_c
                    """
                ),
                {"now_ts": int(now_ts)},
            )
        ).one()
        return UsersListCounts(
            all=int(row[0] or 0),
            active=int(row[1] or 0),
            expired=int(row[2] or 0),
            limited=int(row[3] or 0),
            error=int(row[4] or 0),
        )

    active_c = _active_user_clause(now_ts)
    expired_c = _expired_user_clause(now_ts)
    limited_c = _limited_user_clause(now_ts)
    id_count = (
        func.count(func.distinct(User.id)) if needs_group_join else func.count()
    )
    q = select(
        id_count,
        id_count.filter(active_c),
        id_count.filter(expired_c),
        id_count.filter(limited_c),
    )
    if needs_group_join:
        q = q.join(UserGroup, UserGroup.user_id == User.id)
        if group_filter is not None:
            filters.append(group_filter)
    if filters:
        q = q.where(*filters)
    row = (await db.execute(q)).one()
    # Error chip scoped: same actionable filter as _error_user_clause.
    from sqlalchemy import not_

    err_q = (
        select(func.count(func.distinct(User.id)))
        .select_from(PanelAccount)
        .join(Panel, Panel.id == PanelAccount.panel_id)
        .join(User, User.id == PanelAccount.user_id)
        .where(
            PanelAccount.status == PanelAccountStatus.error,
            Panel.is_active.is_(True),
            active_c,
            or_(
                PanelAccount.links.is_(None),
                PanelAccount.links == [],
            ),
            or_(
                PanelAccount.error_message.is_(None),
                PanelAccount.error_message == "",
                and_(
                    PanelAccount.error_message != "ASTRA_QUOTA_FROZEN",
                    not_(_capacity_error_sql()),
                    not_(_soft_timeout_error_sql()),
                    not_(_db_busy_error_sql()),
                ),
            ),
        )
    )
    if needs_group_join:
        err_q = err_q.join(UserGroup, UserGroup.user_id == User.id)
    if filters:
        err_q = err_q.where(*filters)
    err_n = int((await db.execute(err_q)).scalar() or 0)
    return UsersListCounts(
        all=int(row[0] or 0),
        active=int(row[1] or 0),
        error=err_n,
        expired=int(row[2] or 0),
        limited=int(row[3] or 0),
    )


@router.get("/api/users/counts", response_model=UsersListCounts)
async def users_list_counts(
    request: Request,
    search: str | None = None,
    group_id: int | None = Query(None, ge=1),
    token: str | None = Depends(oauth2_scheme),
):
    """Chip totals only — loaded after the users list so the board paints fast."""
    async with _USERS_BOARD_SEM:
        from app.core.database import AsyncSessionLocal, release_db_txn

        async with AsyncSessionLocal() as db:
            await get_current_admin(request, token, db)
            counts = await _users_list_chip_counts(
                db,
                search=search,
                group_id=group_id,
                now_ts=int(time.time()),
            )
            await release_db_txn(db)
            return counts


@router.get("/api/users", response_model=UsersListResponse)
async def list_users(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    search: str | None = None,
    group_id: int | None = Query(None, ge=1),
    filter: str | None = Query(
        None,
        pattern="^(all|active|error|expired|limited)$",
        description="Chip filter across all pages: all|active|error|expired|limited",
    ),
    refresh: bool = Query(
        False,
        description="Pull live usage from remote panels for this page (read-only; no remote push)",
    ),
    include_counts: bool = Query(
        False,
        description="Include chip totals in this response (slow). Prefer GET /api/users/counts.",
    ),
    details: bool = Query(
        True,
        description="Include panel_accounts for cards. False = lean rows for first paint.",
    ),
    token: str | None = Depends(oauth2_scheme),
):
    # Acquire the list gate BEFORE opening a DB session — otherwise bot stampedes
    # hold dozens of idle-in-transaction checkouts while queued on the semaphore.
    is_search = bool((search or "").strip())
    gate = _USERS_SEARCH_SEM if is_search else _USERS_BOARD_SEM
    async with gate:
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
                include_counts=include_counts,
                details=details,
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
    include_counts: bool,
    details: bool,
    db: AsyncSession,
):
    now_ts = int(time.time())
    status_filter = (filter or "all").strip().lower()
    if status_filter not in ("all", "active", "error", "expired", "limited"):
        status_filter = "all"
    is_search = bool((search or "").strip())
    # Cap search page size — prefix typeahead can match many rows.
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
    limited_c = _limited_user_clause(now_ts)

    status_extra = None
    if status_filter == "active":
        status_extra = active_c
    elif status_filter == "error":
        status_extra = error_c
    elif status_filter == "expired":
        status_extra = expired_c
    elif status_filter == "limited":
        status_extra = limited_c

    # details=true always loads panel status (board + search). Search used to
    # force lean-only and left cards stuck at ۰/N after bulk provision finished.
    want_details = bool(details)
    lean_user = load_only(
        User.id,
        User.username,
        User.display_name,
        User.status,
        User.data_limit,
        User.used_traffic,
        User.lifetime_used_traffic,
        User.expire,
        User.on_hold_expire_duration,
        User.include_promo_first,
        User.data_limit_reset_strategy,
        User.note,
        User.created_at,
        User.sub_token,
        User.source_category_id,
    )
    panel_account_opts = (
        selectinload(User.panel_accounts).options(
            load_only(
                PanelAccount.id,
                PanelAccount.user_id,
                PanelAccount.panel_id,
                PanelAccount.group_id,
                PanelAccount.remote_username,
                PanelAccount.status,
                PanelAccount.error_message,
                PanelAccount.used_traffic,
                PanelAccount.coeff_anchor_raw,
                PanelAccount.coeff_anchor_weighted,
                PanelAccount.applied_coefficient,
                PanelAccount.last_sync_at,
            ),
            defer(PanelAccount.links),
            defer(PanelAccount.links_by_group),
            selectinload(PanelAccount.panel).load_only(
                Panel.id,
                Panel.name,
                Panel.type,
                Panel.is_active,
                Panel.base_url,
            ),
        )
        if want_details
        else None
    )
    if is_search:
        # Search: COUNT + lean rows; optional panel status for card chips.
        # Exact username path stays indexed so bots find users on main panels.
        total = await _count_users(
            db, search=search, group_id=group_id, extra=status_extra
        )
        # Chips are for the full board; search replies only need matching total.
        counts = UsersListCounts(all=total, active=0, error=0, expired=0)
        opts = [
            lean_user,
            selectinload(User.groups).load_only(Group.id),
        ]
        if panel_account_opts is not None:
            opts.append(panel_account_opts)
        q = select(User).options(*opts)
    else:
        # Fast path: page total only. Chip breakdown arrives via /api/users/counts.
        if include_counts:
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
            elif status_filter == "limited":
                total = counts.limited
            else:
                total = await _count_users(
                    db, search=search, group_id=group_id, extra=status_extra
                )
        else:
            total = await _count_users(
                db, search=search, group_id=group_id, extra=status_extra
            )
            counts = UsersListCounts(all=total if status_filter == "all" else 0)

        if want_details and panel_account_opts is not None:
            # Full cards: panel meters (still defer huge share-link JSON).
            q = select(User).options(
                lean_user,
                selectinload(User.groups).load_only(Group.id),
                panel_account_opts,
            )
        else:
            # First paint: usernames + traffic only — panel rows hydrate next.
            q = select(User).options(
                lean_user,
                selectinload(User.groups).load_only(Group.id),
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
    if want_details:
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

    if refresh and users and want_details:
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

    # Serialize WHILE the session is still usable — release_db_txn/rollback
    # expires ORM attrs and to_user_response would MissingGreenlet on lazy IO.
    share_meta: dict[int, tuple[bool, int]] = {}
    if want_details and users:
        try:
            from app.services.users import panel_account_share_meta

            aid_list: list[int] = []
            for u in users:
                for a in u.__dict__.get("panel_accounts") or []:
                    aid = a.__dict__.get("id")
                    if aid:
                        aid_list.append(int(aid))
            share_meta = await panel_account_share_meta(db, aid_list)
        except Exception:  # noqa: BLE001
            share_meta = {}
    payload = UsersListResponse(
        users=[
            to_user_response(u, include_links=False, share_meta=share_meta)
            for u in users
        ],
        total=total,
        counts=counts,
    )
    from app.core.database import release_db_txn

    await release_db_txn(db)
    return payload


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
