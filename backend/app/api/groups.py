import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_admin
from app.core.database import get_db
from app.models.group import Group, GroupCategory, GroupPanel, UserGroup
from app.models.group_config import GroupConfigItem
from app.models.panel import Panel
from app.schemas.group import (
    GroupCategoryCreate,
    GroupCategoryOut,
    GroupCategoryUpdate,
    GroupCreate,
    GroupMoveBody,
    GroupOut,
    GroupPanelOut,
    GroupReorderBody,
    GroupUpdate,
)
from app.services.group_configs import sample_username_for_group
from app.services.link_remark import normalize_notices

router = APIRouter(tags=["Group"])
logger = logging.getLogger(__name__)


def _normalize_panel_config(config: Any) -> dict[str, Any]:
    """Persist a clean panel config; coefficient always a float (e.g. 1.33)."""
    from app.services.quota import norm_coeff

    cfg: dict[str, Any] = dict(config) if isinstance(config, dict) else {}
    cfg["coefficient"] = float(norm_coeff(cfg.get("coefficient", 1)))
    lip = cfg.get("limit_ip", cfg.get("ip_limit", 0))
    try:
        n = int(lip or 0)
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        cfg["limit_ip"] = n
    else:
        cfg.pop("limit_ip", None)
        cfg.pop("ip_limit", None)
    return cfg


def _panel_config_fingerprint(panels: list[Any]) -> tuple[tuple[int, str], ...]:
    """Membership + full config (including coefficient)."""
    items: list[tuple[int, str]] = []
    for item in panels or []:
        pid = int(getattr(item, "panel_id"))
        cfg = _normalize_panel_config(getattr(item, "config", None) or {})
        items.append(
            (pid, json.dumps(cfg, sort_keys=True, default=str, ensure_ascii=False))
        )
    return tuple(sorted(items, key=lambda x: (x[0], x[1])))


def _panel_remote_fingerprint(panels: list[Any]) -> tuple[tuple[int, str], ...]:
    """Membership + remote-affecting config (coefficient is local-only)."""
    items: list[tuple[int, str]] = []
    for item in panels or []:
        pid = int(getattr(item, "panel_id"))
        cfg = _normalize_panel_config(getattr(item, "config", None) or {})
        cfg.pop("coefficient", None)
        items.append(
            (pid, json.dumps(cfg, sort_keys=True, default=str, ensure_ascii=False))
        )
    return tuple(sorted(items, key=lambda x: (x[0], x[1])))


async def _bg_resync_group_users(
    group_id: int, removed_panel_ids: list[int] | None = None
) -> None:
    """Push new caps/coeffs + remote delete for removed panels (non-blocking).

    Uses its own DB session and commits progress inside resync so a long run
    does not crash the request pool or lose all work on failure.

    If a resync is already running (e.g. panel added, then quickly removed),
    mark dirty so the active wave cancels and re-applies latest membership.
    """
    from app.core.database import AsyncSessionLocal
    from app.services.orchestrator import (
        group_resync_in_progress,
        mark_group_resync_dirty,
        resync_users_for_group,
    )

    if group_resync_in_progress(group_id):
        mark_group_resync_dirty(group_id, removed_panel_ids or [])
        logger.info(
            "bg group %s resync queued restart removed=%s",
            group_id,
            removed_panel_ids or [],
        )
        return
    try:
        async with AsyncSessionLocal() as db:
            n = await resync_users_for_group(
                db, group_id, removed_panel_ids=removed_panel_ids or []
            )
            # resync commits internally; final commit is harmless if idle
            try:
                await db.commit()
            except Exception:  # noqa: BLE001
                try:
                    await db.rollback()
                except Exception:  # noqa: BLE001
                    pass
            logger.info(
                "bg group %s resync ok users=%s removed_panels=%s",
                group_id,
                n,
                removed_panel_ids or [],
            )
    except Exception:  # noqa: BLE001
        logger.exception("bg group %s resync failed", group_id)


async def _bg_apply_group_panels_to_subs(
    group_id: int,
    *,
    category_id: int | None,
    group_name: str = "",
    removed_panel_ids: list[int] | None = None,
) -> None:
    """Apply group panels to EVERY relevant user — category first, then group.

    Previously category + group resyncs ran in parallel. Group work-list is
    UserGroup members only, so category-bound users were still missing
    membership when the group wave started → empty/partial apply + dashboard
    «کاربری در گروه نبود» / thin remotes on the main panel.
    """
    from app.services.ops_progress import start_job

    gid = int(group_id)
    cat_id = int(category_id) if category_id is not None else None
    gname = (group_name or "").strip()

    if cat_id is not None:
        title = (
            f"۱/۲ عضویت دسته — گروه «{gname}»"
            if gname
            else f"۱/۲ عضویت دسته برای گروه #{gid}"
        )
        jid = start_job(
            "category_resync",
            title,
            message="اول همه کاربران دسته عضو گروه می‌شوند…",
        )
        # Full desired groups (no added-only hint) so every bound user gets
        # complete membership before panel delete/add/retry.
        await _bg_resync_category_users(cat_id, job_id=jid)

    await _bg_resync_group_users(gid, removed_panel_ids)


def _schedule_category_resync(
    category_id: int,
    *,
    removed_group_ids: list[int] | None = None,
    added_group_ids: list[int] | None = None,
    group_name: str = "",
    defer_seconds: float = 0.2,
) -> None:
    """Register dashboard job immediately, then run resync in background.

    Heavy work is deferred slightly so the HTTP handler can finish and release
    its DB session before the bg task opens another (avoids pool wait on create).
    """
    from app.services.ops_progress import start_job

    cid = int(category_id)
    gname = (group_name or "").strip()
    if added_group_ids and not removed_group_ids:
        title = (
            f"افزودن گروه «{gname}» به ساب کاربران دسته"
            if gname
            else f"افزودن گروه به ساب کاربران دسته #{cid}"
        )
        msg = "در صف — پنل‌های گروه روی ساب کاربران این دسته اضافه می‌شود…"
    elif removed_group_ids and not added_group_ids:
        title = (
            f"حذف گروه «{gname}» از ساب کاربران دسته"
            if gname
            else f"حذف گروه از ساب کاربران دسته #{cid}"
        )
        msg = "در صف — پنل‌های گروه از ساب کاربران این دسته حذف می‌شود…"
    else:
        title = f"همگام‌سازی ساب کاربران دسته #{cid}"
        msg = "در صف — همگام‌سازی عضویت گروه روی ساب…"
    job_id = start_job("category_resync", title, message=msg)
    rem = list(removed_group_ids) if removed_group_ids else None
    add = list(added_group_ids) if added_group_ids else None
    delay = max(0.0, float(defer_seconds or 0))

    async def _kick() -> None:
        if delay:
            await asyncio.sleep(delay)
        await _bg_resync_category_users(
            cid,
            removed_group_ids=rem,
            added_group_ids=add,
            job_id=job_id,
        )

    asyncio.create_task(_kick(), name=f"astra-cat-resync-{cid}-{job_id}")


async def _bg_resync_category_users(
    *category_ids: int | None,
    removed_group_ids: list[int] | None = None,
    added_group_ids: list[int] | None = None,
    job_id: str | None = None,
) -> None:
    """Sync users bound to sales categories after membership change."""
    from app.core.database import AsyncSessionLocal
    from app.services.ops_progress import finish_job_id, start_job, update_job
    from app.services.orchestrator import resync_users_for_category

    seen: set[int] = set()
    for raw in category_ids:
        if raw is None:
            continue
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue
        if cid in seen:
            continue
        seen.add(cid)
        jid = job_id
        if not jid:
            bits: list[str] = []
            if removed_group_ids:
                bits.append("حذف از ساب")
            if added_group_ids:
                bits.append("اضافه به ساب")
            title = (
                f"همگام‌سازی ساب کاربران دسته #{cid}"
                + (f" — {' و '.join(bits)}" if bits else "")
            )
            jid = start_job(
                "category_resync",
                title,
                message="شروع همگام‌سازی دسته روی ساب…",
            )
        else:
            update_job(jid, message="شروع همگام‌سازی دسته روی ساب…")
        try:
            async with AsyncSessionLocal() as db:
                n = await resync_users_for_category(
                    db,
                    cid,
                    removed_group_ids=removed_group_ids,
                    added_group_ids=added_group_ids,
                    job_id=jid,
                )
                await db.commit()
                # finish_job_id already called inside resync_users_for_category
                logger.info("bg category %s resync ok changed_users=%s", cid, n)
        except Exception:  # noqa: BLE001
            logger.exception("bg category %s resync failed", cid)
            finish_job_id(
                jid, failed=1, message="همگام‌سازی دسته با خطا متوقف شد"
            )
        # Only the first category uses the pre-created job_id
        job_id = None


async def _bg_move_group_between_categories(
    group_id: int,
    old_category_id: int | None,
    new_category_id: int | None,
    *,
    group_name: str = "",
) -> None:
    """Leave old category (delete remotes) then join new (provision) — sequential."""
    gid = int(group_id)
    gname = (group_name or "").strip() or f"#{gid}"
    # Register both cards up-front so dashboard shows the full plan
    try:
        if old_category_id is not None:
            from app.services.ops_progress import start_job

            leave_id = start_job(
                "category_resync",
                f"حذف گروه «{gname}» از ساب دسته قبلی",
                message="در صف — حذف پنل‌ها از ساب و پنل اصلی (مصرف حفظ می‌شود)…",
            )
            # Membership → desired of old category WITHOUT this group, and
            # force-cut remotes for ``gid`` (used_traffic kept on deleted rows).
            await _bg_resync_category_users(
                int(old_category_id),
                removed_group_ids=[gid],
                job_id=leave_id,
            )
        if new_category_id is not None:
            # Join = full category membership for bound users, then this
            # group's panel delete/add/retry (same as manual «اعمال پنل‌ها»).
            await _bg_apply_group_panels_to_subs(
                gid,
                category_id=int(new_category_id),
                group_name=gname,
                removed_panel_ids=[],
            )
    except Exception:  # noqa: BLE001
        logger.exception("bg move group %s between categories failed", gid)
        raise


async def _bg_recount_group_users(group_id: int) -> None:
    """Coefficient-only save: recount Astra usage, freeze remotes that flipped."""
    from sqlalchemy.orm import selectinload

    from app.core.database import AsyncSessionLocal
    from app.models.user import PanelAccount, User
    from app.services.ops_progress import finish_job_id, start_job
    from app.services.orchestrator import recount_group_usage_local, sync_modify_user

    job_id = start_job(
        "recount",
        f"بازشماری مصرف گروه #{int(group_id)}",
        message="در حال محاسبه مصرف با ضریب جدید…",
    )
    flipped: list[int] = []
    try:
        async with AsyncSessionLocal() as db:
            flipped = await recount_group_usage_local(db, group_id)
            await db.commit()
            logger.info(
                "bg group %s coeff recount flipped=%s", group_id, len(flipped)
            )
    except Exception:  # noqa: BLE001
        logger.exception("bg group %s coeff recount failed", group_id)
        finish_job_id(job_id, failed=1, message="بازشماری مصرف ناموفق بود")
        return

    for uid in flipped:
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(User)
                    .where(User.id == uid)
                    .options(
                        selectinload(User.panel_accounts).selectinload(
                            PanelAccount.panel
                        )
                    )
                )
                user = result.scalar_one_or_none()
                if user is None:
                    continue
                await sync_modify_user(
                    db, user, remote_timeout=10, skip_nginx_sync=True
                )
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.warning(
                "bg group %s freeze after coeff user_id=%s failed",
                group_id,
                uid,
                exc_info=True,
            )
    finish_job_id(
        job_id,
        ok=len(flipped),
        message=f"بازشماری تمام شد — {len(flipped)} کاربر به‌روز شد",
    )


def _normalize_config_tag(raw: str | None) -> str:
    # Same clip as sub remarks — keep x1.2 / ۱.۵ intact (never silent [:24]).
    from app.services.link_remark import _clip_config_tag

    return _clip_config_tag(raw)


def _normalize_usage_cost(raw: int | None) -> int | None:
    if raw is None:
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if n < 0:
        raise ValueError("هزینه مصرف نمی‌تواند منفی باشد")
    if n > 1_000_000:
        raise ValueError("هزینه مصرف خیلی بزرگ است")
    return n


def _normalize_order_kind(raw: str | None) -> str:
    from app.services.ping_category_order import normalize_order_kind

    return normalize_order_kind(raw)


def _normalize_category_name(raw: str | None) -> str:
    text = " ".join(str(raw or "").split()).strip()
    return text[:48]


def _to_out(group: Group) -> GroupOut:
    panels = []
    for link in group.panel_links or []:
        panels.append(
            GroupPanelOut(
                panel_id=link.panel_id,
                config=link.config or {},
                priority=link.priority,
                panel_name=link.panel.name if link.panel else None,
                panel_type=link.panel.type.value if link.panel else None,
                panel_is_active=bool(link.panel.is_active) if link.panel else True,
            )
        )
    cost = getattr(group, "usage_cost", None)
    try:
        cost_out = int(cost) if cost is not None else None
    except (TypeError, ValueError):
        cost_out = None
    try:
        sort_order = int(getattr(group, "sort_order", 0) or 0)
    except (TypeError, ValueError):
        sort_order = 0
    return GroupOut(
        id=group.id,
        name=group.name,
        description=group.description,
        is_disabled=group.is_disabled,
        config_tag=_normalize_config_tag(getattr(group, "config_tag", None) or ""),
        usage_cost=cost_out,
        order_kind=_normalize_order_kind(
            getattr(group, "order_kind", None) or "other"
        ),
        notices=list(group.notices or []),
        category_id=getattr(group, "category_id", None),
        sort_order=sort_order,
        inbound_tags=[],
        panels=panels,
        created_at=group.created_at,
    )


async def _next_group_sort_order(
    db: AsyncSession, category_id: int | None
) -> int:
    q = select(func.coalesce(func.max(Group.sort_order), 0))
    if category_id is None:
        q = q.where(Group.category_id.is_(None))
    else:
        q = q.where(Group.category_id == int(category_id))
    raw = (await db.execute(q)).scalar()
    try:
        return int(raw or 0) + 10
    except (TypeError, ValueError):
        return 10


async def _load_group(db: AsyncSession, group_id: int) -> Group | None:
    result = await db.execute(
        select(Group)
        .where(Group.id == group_id)
        .options(selectinload(Group.panel_links).selectinload(GroupPanel.panel))
    )
    return result.scalar_one_or_none()


async def _ensure_category(db: AsyncSession, category_id: int | None) -> int | None:
    if category_id is None:
        return None
    cat = await db.get(GroupCategory, int(category_id))
    if not cat:
        raise HTTPException(status_code=400, detail="دسته پیدا نشد")
    return int(cat.id)


async def _set_panels(db: AsyncSession, group: Group, panels: list) -> None:
    # Explicit query — never lazy-load panel_links in async (MissingGreenlet)
    existing = await db.execute(select(GroupPanel).where(GroupPanel.group_id == group.id))
    for link in existing.scalars().all():
        await db.delete(link)
    await db.flush()
    if not panels:
        return
    wanted = [int(item.panel_id) for item in panels]
    # select(Panel.id) yields bare ints from scalars(), not Panel rows
    found = {
        int(pid)
        for pid in (
            await db.execute(select(Panel.id).where(Panel.id.in_(wanted)))
        ).scalars().all()
    }
    missing = next((pid for pid in wanted if pid not in found), None)
    if missing is not None:
        raise HTTPException(status_code=400, detail=f"Panel {missing} not found")
    for item in panels:
        db.add(
            GroupPanel(
                group_id=group.id,
                panel_id=int(item.panel_id),
                config=_normalize_panel_config(item.config),
                priority=item.priority,
            )
        )


# ── Categories ──────────────────────────────────────────────────────────────


@router.get("/api/group-categories", response_model=list[GroupCategoryOut])
async def list_group_categories(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    counts = {
        int(cid): int(n)
        for cid, n in (
            await db.execute(
                select(Group.category_id, func.count())
                .where(Group.category_id.is_not(None))
                .group_by(Group.category_id)
            )
        ).all()
        if cid is not None
    }
    rows = (
        await db.execute(select(GroupCategory).order_by(GroupCategory.sort_order, GroupCategory.id))
    ).scalars().all()
    return [
        GroupCategoryOut(
            id=c.id,
            name=c.name,
            sort_order=c.sort_order,
            groups_count=counts.get(c.id, 0),
            created_at=c.created_at,
        )
        for c in rows
    ]


@router.post("/api/group-categories", response_model=GroupCategoryOut)
async def create_group_category(
    body: GroupCategoryCreate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    name = _normalize_category_name(body.name)
    if len(name) < 1:
        raise HTTPException(status_code=400, detail="نام دسته خالی است")
    exists = await db.execute(select(GroupCategory).where(GroupCategory.name == name))
    if exists.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="دسته با این نام از قبل هست")
    max_ord = (
        await db.execute(select(func.max(GroupCategory.sort_order)))
    ).scalar()
    cat = GroupCategory(
        name=name,
        sort_order=body.sort_order if body.sort_order else int(max_ord or 0) + 1,
    )
    db.add(cat)
    await db.commit()
    await db.refresh(cat)
    return GroupCategoryOut(
        id=cat.id,
        name=cat.name,
        sort_order=cat.sort_order,
        groups_count=0,
        created_at=cat.created_at,
    )


@router.put("/api/group-categories/{category_id}", response_model=GroupCategoryOut)
async def update_group_category(
    category_id: int,
    body: GroupCategoryUpdate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    cat = await db.get(GroupCategory, category_id)
    if not cat:
        raise HTTPException(status_code=404, detail="دسته پیدا نشد")
    if body.name is not None:
        name = _normalize_category_name(body.name)
        if len(name) < 1:
            raise HTTPException(status_code=400, detail="نام دسته خالی است")
        clash = await db.execute(
            select(GroupCategory).where(
                GroupCategory.name == name, GroupCategory.id != category_id
            )
        )
        if clash.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="دسته با این نام از قبل هست")
        cat.name = name
    if body.sort_order is not None:
        cat.sort_order = int(body.sort_order)
    await db.commit()
    await db.refresh(cat)
    n = (
        await db.execute(
            select(func.count()).select_from(Group).where(Group.category_id == cat.id)
        )
    ).scalar() or 0
    return GroupCategoryOut(
        id=cat.id,
        name=cat.name,
        sort_order=cat.sort_order,
        groups_count=int(n),
        created_at=cat.created_at,
    )


@router.delete("/api/group-categories/{category_id}")
async def delete_group_category(
    category_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Delete the category tab only — never delete sales groups.

    All groups in this category are moved to the default «همه گروه‌ها» bucket
    (category_id = NULL).
    """
    cat = await db.get(GroupCategory, category_id)
    if not cat:
        raise HTTPException(status_code=404, detail="دسته پیدا نشد")
    # Move groups first (explicit) — groups must survive
    moved = await db.execute(
        update(Group)
        .where(Group.category_id == category_id)
        .values(category_id=None)
        .execution_options(synchronize_session="fetch")
    )
    await db.flush()
    await db.delete(cat)
    await db.commit()
    return {
        "detail": "Category deleted",
        "groups_moved_to_default": int(moved.rowcount or 0),
    }


async def _bg_full_category_apply_to_subs(category_id: int) -> None:
    """Membership for all bound users, then panel wave for every group in the category.

    Category-only membership heal was leaving remotes thin after new groups /
    panel edits — admins expect «همگام‌سازی ساب کاربران» to fully land configs.
    """
    from app.core.database import AsyncSessionLocal
    from app.services.ops_progress import finish_job_id, start_job
    from app.services.orchestrator import category_group_ids

    cid = int(category_id)
    jid = start_job(
        "category_resync",
        f"۱/۲ عضویت کاربران دسته #{cid}",
        message="اول همه کاربران دسته با گروه‌های فعلی هم‌تراز می‌شوند…",
    )
    try:
        await _bg_resync_category_users(cid, job_id=jid)
    except Exception:  # noqa: BLE001
        logger.exception("full category %s membership step failed", cid)
        finish_job_id(jid, failed=1, message="همگام عضویت دسته ناموفق بود")
        return

    try:
        async with AsyncSessionLocal() as db:
            gids = await category_group_ids(db, cid)
    except Exception:  # noqa: BLE001
        logger.exception("full category %s list groups failed", cid)
        return

    for gid in gids:
        try:
            await _bg_resync_group_users(int(gid), [])
        except Exception:  # noqa: BLE001
            logger.exception(
                "full category %s group %s panel wave failed", cid, gid
            )


@router.post("/api/group-categories/{category_id}/resync-users")
async def resync_category_users(
    category_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Force-sync all category-bound users to current groups (subs update)."""
    cat = await db.get(GroupCategory, category_id)
    if not cat:
        raise HTTPException(status_code=404, detail="دسته پیدا نشد")
    # Membership + every group's panel delete/add/retry (not membership-only).
    asyncio.create_task(
        _bg_full_category_apply_to_subs(int(category_id)),
        name=f"astra-cat-full-apply-{int(category_id)}",
    )
    return {
        "ok": True,
        "detail": (
            "همگام‌سازی کاربران این دسته شروع شد "
            "(عضویت + اعمال پنل هر گروه روی ساب) — پیشرفت در داشبورد"
        ),
        "category_id": int(category_id),
    }


# ── Groups ──────────────────────────────────────────────────────────────────


@router.post("/api/group", response_model=GroupOut)
async def create_group(
    body: GroupCreate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    existing = await db.execute(select(Group).where(Group.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Group already exists")
    category_id = await _ensure_category(db, body.category_id)
    try:
        usage_cost = _normalize_usage_cost(body.usage_cost)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    sort_order = await _next_group_sort_order(db, category_id)
    group = Group(
        name=body.name,
        description=body.description,
        is_disabled=body.is_disabled,
        config_tag=_normalize_config_tag(body.config_tag),
        usage_cost=usage_cost,
        order_kind=_normalize_order_kind(body.order_kind),
        notices=normalize_notices(body.notices),
        category_id=category_id,
        sort_order=sort_order,
    )
    db.add(group)
    await db.flush()
    if body.panels:
        await _set_panels(db, group, body.panels)
    await db.commit()
    # Load response first; defer heavy apply so this request releases the pool.
    loaded = await _load_group(db, group.id)
    out = _to_out(loaded)  # type: ignore[arg-type]
    if category_id is not None:
        # Must be category-first THEN group panel wave — category-only resync
        # left membership thin / remotes pending for source_category users.
        gid = int(group.id)
        gname = str(group.name or "")
        cid = int(category_id)

        async def _kick_create_apply() -> None:
            await asyncio.sleep(0.35)
            await _bg_apply_group_panels_to_subs(
                gid,
                category_id=cid,
                group_name=gname,
                removed_panel_ids=[],
            )

        asyncio.create_task(
            _kick_create_apply(),
            name=f"astra-group-create-apply-{gid}",
        )
    return out


@router.get("/api/group", response_model=list[GroupOut])
@router.get("/api/groups", response_model=list[GroupOut])
async def list_groups(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    result = await db.execute(
        select(Group)
        .options(selectinload(Group.panel_links).selectinload(GroupPanel.panel))
        .order_by(Group.sort_order.asc(), Group.id.asc())
    )
    return [_to_out(g) for g in result.scalars().all()]


@router.put("/api/groups/reorder", response_model=list[GroupOut])
@router.put("/api/group/reorder", response_model=list[GroupOut])
async def reorder_groups(
    body: GroupReorderBody,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Persist drag order for one Groups-page tab (UI only)."""
    ids = [int(x) for x in body.group_ids if x is not None]
    if not ids:
        raise HTTPException(status_code=400, detail="لیست گروه خالی است")
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=400, detail="شناسه تکراری در ترتیب")

    cat_id = await _ensure_category(db, body.category_id)
    if cat_id is None:
        scope = await db.execute(select(Group).where(Group.category_id.is_(None)))
    else:
        scope = await db.execute(
            select(Group).where(Group.category_id == int(cat_id))
        )
    scoped = {int(g.id): g for g in scope.scalars().all()}
    if set(ids) != set(scoped.keys()):
        raise HTTPException(
            status_code=400,
            detail="ترتیب باید دقیقاً همه گروه‌های همین تب را شامل شود",
        )
    for i, gid in enumerate(ids):
        scoped[gid].sort_order = (i + 1) * 10
    await db.commit()
    rows = await db.execute(
        select(Group)
        .where(Group.id.in_(ids))
        .options(selectinload(Group.panel_links).selectinload(GroupPanel.panel))
        .order_by(Group.sort_order.asc(), Group.id.asc())
    )
    return [_to_out(g) for g in rows.scalars().all()]


@router.get("/api/group/{group_id}", response_model=GroupOut)
async def get_group(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    group = await _load_group(db, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return _to_out(group)


@router.post("/api/group/{group_id}/resync-users")
async def resync_group_users(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Apply current group panel membership to every user's remotes + sub.

    - Panels removed from the group → delete accounts on those panels
    - Panels added → provision + hydrate share links (frozen if Astra is limited)
    - If the group belongs to a sales category → also sync ALL users bound to
      that category (source_category_id) so a new group actually lands on their sub
    Runs in background so the admin UI stays responsive even for large groups.
    """
    from app.models.user import User as UserModel
    from app.services.orchestrator import (
        group_resync_in_progress,
        mark_group_resync_dirty,
    )

    group = await db.get(Group, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="گروه پیدا نشد")
    ug = await db.execute(
        select(UserGroup.user_id).where(UserGroup.group_id == int(group_id))
    )
    member_ids = {int(r[0]) for r in ug.all() if r[0] is not None}
    user_count = len(member_ids)
    cat_id = getattr(group, "category_id", None)
    cat_users = 0
    if cat_id is not None:
        crow = await db.execute(
            select(func.count())
            .select_from(UserModel)
            .where(UserModel.source_category_id == int(cat_id))
        )
        cat_users = int(crow.scalar() or 0)

    if group_resync_in_progress(group_id):
        mark_group_resync_dirty(int(group_id), [])

        async def _bg_refresh_membership_then_dirty() -> None:
            """Finish category bind, then force the running group wave to redo."""
            if cat_id is None:
                return
            from app.services.ops_progress import start_job
            from app.services.orchestrator import mark_group_resync_dirty as _dirty

            jid = start_job(
                "category_resync",
                f"عضویت دسته قبل از اعمال مجدد «{getattr(group, 'name', '')}»",
                message="کاربران دسته عضو می‌شوند تا موج بعدی گروه همه را ببیند…",
            )
            await _bg_resync_category_users(int(cat_id), job_id=jid)
            _dirty(int(group_id), [])

        if cat_id is not None:
            asyncio.create_task(
                _bg_refresh_membership_then_dirty(),
                name=f"astra-group-resync-cat-refresh-{group_id}",
            )
        return {
            "ok": True,
            "started": False,
            "queued": True,
            "detail": (
                "همگام‌سازی قبلی قطع می‌شود و با آخرین پنل‌های گروه "
                "از نو روی ساب اعمال می‌شود"
                + (
                    f" — اول {cat_users} کاربر دسته، بعد حذف/اضافه/تلاش خطا"
                    if cat_id is not None
                    else ""
                )
                + " — داشبورد را ببینید"
            ),
            "group_id": int(group_id),
            "users": user_count,
            "category_users": cat_users,
        }

    # Sequential: category membership → group panel delete/add/retry.
    # Never fire both in parallel (that left category users unprovisioned).
    asyncio.create_task(
        _bg_apply_group_panels_to_subs(
            int(group_id),
            category_id=int(cat_id) if cat_id is not None else None,
            group_name=str(getattr(group, "name", "") or ""),
            removed_panel_ids=[],
        ),
        name=f"astra-group-manual-resync-{group_id}",
    )
    detail = (
        f"اعمال پنل‌ها روی ساب شروع شد"
        f" ({user_count} عضو گروه"
        + (f" + {cat_users} کاربر دسته" if cat_id is not None else "")
        + ")"
    )
    if cat_id is not None:
        detail += " — مرحله ۱ عضویت دسته، مرحله ۲ حذف/اضافه/تلاش خطا"
    detail += " — پیشرفت زنده در داشبورد"
    return {
        "ok": True,
        "started": True,
        "detail": detail,
        "group_id": int(group_id),
        "users": user_count,
        "category_users": cat_users,
    }


@router.put("/api/group/{group_id}/move", response_model=GroupOut)
async def move_group(
    group_id: int,
    body: GroupMoveBody,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    group = await _load_group(db, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    old_cat = getattr(group, "category_id", None)
    new_cat = await _ensure_category(db, body.category_id)
    group.category_id = new_cat
    if new_cat != old_cat:
        group.sort_order = await _next_group_sort_order(db, new_cat)
    await db.commit()
    if old_cat != new_cat and (old_cat is not None or new_cat is not None):
        gname = str(getattr(group, "name", "") or "")
        # Jobs appear on dashboard immediately (leave then join, sequential in bg)
        asyncio.create_task(
            _bg_move_group_between_categories(
                int(group_id),
                int(old_cat) if old_cat is not None else None,
                int(new_cat) if new_cat is not None else None,
                group_name=gname,
            ),
            name=f"astra-cat-move-{group_id}",
        )
    group = await _load_group(db, group_id)
    return _to_out(group)  # type: ignore[arg-type]


@router.put("/api/group/{group_id}", response_model=GroupOut)
async def update_group(
    group_id: int,
    body: GroupUpdate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    group = await _load_group(db, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    prev_kind = _normalize_order_kind(getattr(group, "order_kind", None) or "other")
    prev_cost = getattr(group, "usage_cost", None)
    layout_changed = False
    if body.name is not None:
        group.name = body.name
    if body.description is not None:
        group.description = body.description
    if body.is_disabled is not None:
        group.is_disabled = body.is_disabled
    if body.config_tag is not None:
        group.config_tag = _normalize_config_tag(body.config_tag)
    if body.clear_usage_cost:
        group.usage_cost = None
        layout_changed = prev_cost is not None
    elif body.usage_cost is not None:
        try:
            group.usage_cost = _normalize_usage_cost(body.usage_cost)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        layout_changed = group.usage_cost != prev_cost
    if body.order_kind is not None:
        group.order_kind = _normalize_order_kind(body.order_kind)
        layout_changed = layout_changed or group.order_kind != prev_kind
    if body.notices is not None:
        group.notices = normalize_notices(body.notices)
    old_cat = getattr(group, "category_id", None)
    cat_changed = False
    if body.clear_category:
        if old_cat is not None:
            cat_changed = True
        group.category_id = None
        if cat_changed:
            group.sort_order = await _next_group_sort_order(db, None)
    elif body.category_id is not None:
        new_cat = await _ensure_category(db, body.category_id)
        if new_cat != old_cat:
            cat_changed = True
            group.sort_order = await _next_group_sort_order(db, new_cat)
        group.category_id = new_cat
    need_full_resync = False
    need_local_recount = False
    removed_panel_ids: list[int] = []
    if body.panels is not None:
        old_links = list(group.panel_links or [])
        old_fp = _panel_config_fingerprint(old_links)
        new_fp = _panel_config_fingerprint(body.panels)
        old_remote_fp = _panel_remote_fingerprint(old_links)
        new_remote_fp = _panel_remote_fingerprint(body.panels)
        old_pids = {int(link.panel_id) for link in old_links if link.panel_id is not None}
        new_pids = {int(getattr(p, "panel_id")) for p in body.panels}
        removed_panel_ids = sorted(old_pids - new_pids)
        await _set_panels(db, group, body.panels)
        need_local_recount = old_fp != new_fp
        need_full_resync = old_remote_fp != new_remote_fp or bool(removed_panel_ids)
    # Disable/enable also affects category-bound membership (enabled-only)
    if body.is_disabled is not None and old_cat is not None:
        cat_changed = True
    await db.commit()
    try:
        from app.api.subscription import invalidate_user_feed_cache
        from app.models.user import UserGroup as UG

        uid_rows = await db.execute(
            select(UG.user_id).where(UG.group_id == int(group_id))
        )
        invalidate_user_feed_cache(
            *[int(r[0]) for r in uid_rows.all() if r[0] is not None]
        )
    except Exception:  # noqa: BLE001
        pass
    # Re-layout config-order in background — never block group save / sub speed
    if layout_changed:
        async def _bg_relayout() -> None:
            from app.core.database import AsyncSessionLocal
            from app.services.config_orders import relayout_order_from_stored

            try:
                async with AsyncSessionLocal() as s:
                    await relayout_order_from_stored(s)
            except Exception:  # noqa: BLE001
                logger.exception("bg relayout after group %s failed", group_id)

        asyncio.create_task(
            _bg_relayout(), name=f"astra-relayout-g{group_id}"
        )
    # Never block PUT /api/group on remote I/O or a full user recount.
    new_cat = getattr(group, "category_id", None)
    gname = str(getattr(group, "name", "") or "")
    # Panel edits on a categorized group must NOT race a parallel category
    # resync — use the same sequential path as «اعمال پنل‌ها روی ساب».
    if need_full_resync and new_cat is not None:
        asyncio.create_task(
            _bg_apply_group_panels_to_subs(
                int(group_id),
                category_id=int(new_cat),
                group_name=gname,
                removed_panel_ids=removed_panel_ids,
            ),
            name=f"astra-group-apply-subs-{group_id}",
        )
        if cat_changed and old_cat is not None and old_cat != new_cat:
            _schedule_category_resync(
                int(old_cat),
                removed_group_ids=[int(group_id)],
                group_name=gname,
            )
    elif need_full_resync:
        asyncio.create_task(
            _bg_resync_group_users(group_id, removed_panel_ids),
            name=f"astra-group-resync-{group_id}",
        )
        if cat_changed:
            if old_cat is not None and old_cat != new_cat:
                _schedule_category_resync(
                    int(old_cat),
                    removed_group_ids=[int(group_id)],
                    group_name=gname,
                )
            if new_cat is not None and new_cat != old_cat:
                _schedule_category_resync(
                    int(new_cat),
                    added_group_ids=[int(group_id)],
                    group_name=gname,
                )
    elif need_local_recount:
        asyncio.create_task(
            _bg_recount_group_users(group_id),
            name=f"astra-group-recount-{group_id}",
        )
        if cat_changed:
            if old_cat is not None and old_cat != new_cat:
                _schedule_category_resync(
                    int(old_cat),
                    removed_group_ids=[int(group_id)],
                    group_name=gname,
                )
            if new_cat is not None and new_cat != old_cat:
                _schedule_category_resync(
                    int(new_cat),
                    added_group_ids=[int(group_id)],
                    group_name=gname,
                )
            elif new_cat is not None and body.is_disabled is not None:
                if bool(body.is_disabled):
                    _schedule_category_resync(
                        int(new_cat),
                        removed_group_ids=[int(group_id)],
                        group_name=gname,
                    )
                else:
                    _schedule_category_resync(
                        int(new_cat),
                        added_group_ids=[int(group_id)],
                        group_name=gname,
                    )
    elif cat_changed:
        if old_cat is not None and old_cat != new_cat:
            _schedule_category_resync(
                int(old_cat),
                removed_group_ids=[int(group_id)],
                group_name=gname,
            )
        if new_cat is not None and new_cat != old_cat:
            # Entering a category → membership + this group's panel wave
            asyncio.create_task(
                _bg_apply_group_panels_to_subs(
                    int(group_id),
                    category_id=int(new_cat),
                    group_name=gname,
                    removed_panel_ids=[],
                ),
                name=f"astra-group-cat-join-{group_id}",
            )
        elif new_cat is not None and body.is_disabled is not None:
            if bool(body.is_disabled):
                _schedule_category_resync(
                    int(new_cat),
                    removed_group_ids=[int(group_id)],
                    group_name=gname,
                )
            else:
                asyncio.create_task(
                    _bg_apply_group_panels_to_subs(
                        int(group_id),
                        category_id=int(new_cat),
                        group_name=gname,
                        removed_panel_ids=[],
                    ),
                    name=f"astra-group-cat-enable-{group_id}",
                )
    group = await _load_group(db, group_id)
    return _to_out(group)  # type: ignore[arg-type]


@router.delete("/api/group/{group_id}")
async def delete_group(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Remove group from Astra AFTER deleting its remotes on every panel.

    Order matters: PanelAccount.group_id is SET NULL when Group is deleted, so
    remotes must be scrubbed while group_id is still set.
    """
    from app.services.orchestrator import deprovision_group_from_all_users

    group = await db.get(Group, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    old_cat = getattr(group, "category_id", None)

    # 1) Fast local cut (bulk) + bg remote delete. Never block on panel HTTP.
    try:
        remote_stats = await deprovision_group_from_all_users(db, group_id)
    except Exception:  # noqa: BLE001
        logger.exception("delete group=%s remote deprovision failed", group_id)
        remote_stats = {"users": 0, "ok": 0, "fail": 0}

    # 2) Explicit child cleanup (avoids async lazy-load / FK 500s)
    await db.execute(delete(GroupConfigItem).where(GroupConfigItem.group_id == group_id))
    await db.execute(delete(GroupPanel).where(GroupPanel.group_id == group_id))
    # UserGroup already wiped in deprovision; belt-and-suspenders
    await db.execute(delete(UserGroup).where(UserGroup.group_id == group_id))

    # Drop local sample probe user if present
    from app.models.user import PanelAccount, User

    sample_name = sample_username_for_group(group_id)
    sample = (
        await db.execute(select(User).where(User.username == sample_name))
    ).scalar_one_or_none()
    if sample is not None:
        await db.execute(delete(PanelAccount).where(PanelAccount.user_id == sample.id))
        await db.execute(delete(UserGroup).where(UserGroup.user_id == sample.id))
        await db.delete(sample)

    await db.delete(group)
    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"حذف گروه ناموفق: {exc}") from exc
    if old_cat is not None:
        # Heal remaining category-bound users to current desired set (bg only)
        _schedule_category_resync(
            int(old_cat),
            removed_group_ids=[int(group_id)],
            group_name=str(getattr(group, "name", "") or ""),
        )
    return {
        "detail": "Group deleted",
        "remote_users": remote_stats.get("users", 0),
        "remote_ok": remote_stats.get("ok", 0),
        "remote_failed": remote_stats.get("fail", 0),
    }
