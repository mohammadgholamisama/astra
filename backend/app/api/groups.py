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
    """Membership + config only (priority ignored) — drives coeff/remote resync."""
    items: list[tuple[int, str]] = []
    for item in panels or []:
        pid = int(getattr(item, "panel_id"))
        cfg = _normalize_panel_config(getattr(item, "config", None) or {})
        items.append(
            (pid, json.dumps(cfg, sort_keys=True, default=str, ensure_ascii=False))
        )
    return tuple(sorted(items, key=lambda x: (x[0], x[1])))


async def _bg_resync_group_users(group_id: int) -> None:
    """Push new caps/coeffs without blocking the group-save HTTP response."""
    from app.core.database import AsyncSessionLocal
    from app.services.orchestrator import resync_users_for_group

    try:
        async with AsyncSessionLocal() as db:
            n = await resync_users_for_group(db, group_id)
            await db.commit()
            logger.info("bg group %s resync ok users=%s", group_id, n)
    except Exception:  # noqa: BLE001
        logger.exception("bg group %s resync failed", group_id)


def _normalize_config_tag(raw: str | None) -> str:
    text = " ".join(str(raw or "").split()).strip()
    text = "".join(ch for ch in text if ord(ch) >= 32 and ch not in "<>\"'`")
    return text[:24]


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
    return GroupOut(
        id=group.id,
        name=group.name,
        description=group.description,
        is_disabled=group.is_disabled,
        config_tag=_normalize_config_tag(getattr(group, "config_tag", None) or ""),
        notices=list(group.notices or []),
        category_id=getattr(group, "category_id", None),
        inbound_tags=[],
        panels=panels,
        created_at=group.created_at,
    )


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
    for item in panels:
        panel = await db.get(Panel, item.panel_id)
        if not panel:
            raise HTTPException(status_code=400, detail=f"Panel {item.panel_id} not found")
        db.add(
            GroupPanel(
                group_id=group.id,
                panel_id=item.panel_id,
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
    group = Group(
        name=body.name,
        description=body.description,
        is_disabled=body.is_disabled,
        config_tag=_normalize_config_tag(body.config_tag),
        notices=normalize_notices(body.notices),
        category_id=category_id,
    )
    db.add(group)
    await db.flush()
    if body.panels:
        await _set_panels(db, group, body.panels)
    await db.commit()
    group = await _load_group(db, group.id)
    return _to_out(group)  # type: ignore[arg-type]


@router.get("/api/group", response_model=list[GroupOut])
@router.get("/api/groups", response_model=list[GroupOut])
async def list_groups(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    result = await db.execute(
        select(Group)
        .options(selectinload(Group.panel_links).selectinload(GroupPanel.panel))
        .order_by(Group.id)
    )
    return [_to_out(g) for g in result.scalars().all()]


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
    group.category_id = await _ensure_category(db, body.category_id)
    await db.commit()
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
    if body.name is not None:
        group.name = body.name
    if body.description is not None:
        group.description = body.description
    if body.is_disabled is not None:
        group.is_disabled = body.is_disabled
    if body.config_tag is not None:
        group.config_tag = _normalize_config_tag(body.config_tag)
    if body.notices is not None:
        group.notices = normalize_notices(body.notices)
    if body.clear_category:
        group.category_id = None
    elif body.category_id is not None:
        group.category_id = await _ensure_category(db, body.category_id)
    need_resync = False
    if body.panels is not None:
        old_fp = _panel_config_fingerprint(list(group.panel_links or []))
        new_fp = _panel_config_fingerprint(body.panels)
        await _set_panels(db, group, body.panels)
        # Only remote-push when panel set / coefficient / inbounds / etc. actually changed
        need_resync = old_fp != new_fp
    if need_resync:
        # Apply raw×coeff to Astra used_traffic immediately (no remote I/O).
        # Bg task still refreshes remotes / disables when quota exhausted.
        from app.services.orchestrator import recount_group_usage_local

        await recount_group_usage_local(db, group_id)
    await db.commit()
    if need_resync:
        # Never block PUT /api/group — large groups time out proxies (502/504).
        asyncio.create_task(
            _bg_resync_group_users(group_id),
            name=f"astra-group-resync-{group_id}",
        )
    group = await _load_group(db, group_id)
    return _to_out(group)  # type: ignore[arg-type]


@router.delete("/api/group/{group_id}")
async def delete_group(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Remove group from Astra only — does not touch remote panels."""
    group = await db.get(Group, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    # Explicit child cleanup (avoids async lazy-load / FK 500s)
    await db.execute(delete(GroupConfigItem).where(GroupConfigItem.group_id == group_id))
    await db.execute(delete(UserGroup).where(UserGroup.group_id == group_id))
    await db.execute(delete(GroupPanel).where(GroupPanel.group_id == group_id))

    # Drop local sample probe user if present (local DB only — no remote delete)
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
    return {"detail": "Group deleted"}
