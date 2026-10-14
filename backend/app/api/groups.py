from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_admin
from app.core.database import get_db
from app.models.group import Group, GroupPanel, UserGroup
from app.models.group_config import GroupConfigItem
from app.models.panel import Panel
from app.schemas.group import GroupCreate, GroupOut, GroupPanelOut, GroupUpdate
from app.services.group_configs import sample_username_for_group
from app.services.link_remark import normalize_notices

router = APIRouter(tags=["Group"])


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
            )
        )
    return GroupOut(
        id=group.id,
        name=group.name,
        description=group.description,
        is_disabled=group.is_disabled,
        notices=list(group.notices or []),
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
                config=item.config or {},
                priority=item.priority,
            )
        )


@router.post("/api/group", response_model=GroupOut)
async def create_group(
    body: GroupCreate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    existing = await db.execute(select(Group).where(Group.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Group already exists")
    group = Group(
        name=body.name,
        description=body.description,
        is_disabled=body.is_disabled,
        notices=normalize_notices(body.notices),
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
        select(Group).options(selectinload(Group.panel_links).selectinload(GroupPanel.panel)).order_by(Group.id)
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
    if body.notices is not None:
        group.notices = normalize_notices(body.notices)
    if body.panels is not None:
        await _set_panels(db, group, body.panels)
    await db.commit()
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
