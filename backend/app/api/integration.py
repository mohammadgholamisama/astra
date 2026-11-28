"""Integration API surface for /docs — groups CRUD + panel sub-rank.

Auth: ``X-Api-Key`` or Bearer JWT (same as other admin APIs).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from pydantic import BaseModel, Field

from app.api.deps import get_current_admin
from app.core.database import get_db
from app.models.group import Group, GroupPanel
from app.models.panel import PanelType
from app.schemas.group import GroupCreate, GroupOut, GroupPanelLink, GroupUpdate
from app.schemas.integration import (
    IntegrationGroupCreate,
    IntegrationGroupOut,
    IntegrationGroupPanelOut,
    IntegrationGroupUpdate,
    PanelsSubStatusResponse,
    PanelSubStatusOut,
)
from app.services import panel_sub_status as sub_status_svc

router = APIRouter(tags=["Integration · API خارجی"])


class PanelTypeInfo(BaseModel):
    type: str = Field(description="مقدار دقیق فیلد `type` هنگام ثبت پنل")
    label: str = Field(description="نام نمایشی")
    description: str = Field(description="توضیح کوتاه برای برنامه‌نویس")


_PANEL_TYPE_DOCS: list[PanelTypeInfo] = [
    PanelTypeInfo(
        type=PanelType.sanaei.value,
        label="سنایی / 3X-UI",
        description="پنل سنایی (3X-UI / x-ui). فیلد type را دقیقاً `sanaei` بگذارید.",
    ),
    PanelTypeInfo(
        type=PanelType.pasarguard.value,
        label="پاسارگاد",
        description="پنل پاسارگاد. فیلد type را دقیقاً `pasarguard` بگذارید.",
    ),
    PanelTypeInfo(
        type=PanelType.marzban.value,
        label="مرزبان",
        description="پنل مرزبان. فیلد type را دقیقاً `marzban` بگذارید.",
    ),
    PanelTypeInfo(
        type=PanelType.rebecca.value,
        label="ربکا",
        description="پنل ربکا. فیلد type را دقیقاً `rebecca` بگذارید.",
    ),
    PanelTypeInfo(
        type=PanelType.guard.value,
        label="گارد",
        description="پنل گارد. فیلد type را دقیقاً `guard` بگذارید.",
    ),
    PanelTypeInfo(
        type=PanelType.astra.value,
        label="آسترا (نود محلی)",
        description="نود Astra خود پنل. فیلد type را دقیقاً `astra` بگذارید.",
    ),
]


def _group_to_integration(g: GroupOut) -> IntegrationGroupOut:
    return IntegrationGroupOut(
        id=int(g.id),
        name=g.name,
        description=g.description,
        is_disabled=bool(g.is_disabled),
        config_tag=g.config_tag or "",
        usage_cost=g.usage_cost,
        order_kind=g.order_kind or "other",
        notices=list(g.notices or []),
        category_id=g.category_id,
        sort_order=int(g.sort_order or 0),
        panels=[
            IntegrationGroupPanelOut(
                panel_id=int(p.panel_id),
                panel_name=p.panel_name,
                panel_type=p.panel_type,
                panel_is_active=bool(p.panel_is_active),
                priority=int(p.priority or 0),
                config=dict(p.config or {}),
            )
            for p in (g.panels or [])
        ],
        created_at=g.created_at,
    )


@router.get(
    "/api/v1/groups",
    response_model=list[IntegrationGroupOut],
    summary="استعلام همه گروه‌ها",
    description=(
        "لیست کامل گروه‌های فروش با پنل‌های متصل.\n\n"
        "**هدر:** `X-Api-Key: pg_key_…` یا Bearer JWT"
    ),
)
async def v1_list_groups(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    from app.api.groups import _to_out

    result = await db.execute(
        select(Group)
        .options(selectinload(Group.panel_links).selectinload(GroupPanel.panel))
        .order_by(Group.sort_order.asc(), Group.id.asc())
    )
    return [_group_to_integration(_to_out(g)) for g in result.scalars().all()]


@router.get(
    "/api/v1/groups/{group_id}",
    response_model=IntegrationGroupOut,
    summary="استعلام یک گروه",
)
async def v1_get_group(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    from app.api.groups import _load_group, _to_out

    g = await _load_group(db, group_id)
    if not g:
        raise HTTPException(status_code=404, detail="گروه پیدا نشد")
    return _group_to_integration(_to_out(g))


@router.post(
    "/api/v1/groups",
    response_model=IntegrationGroupOut,
    summary="افزودن گروه",
    description="گروه جدید می‌سازد و در صورت داشتن `panels` آن‌ها را وصل می‌کند.",
)
async def v1_create_group(
    body: IntegrationGroupCreate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    from app.api import groups as groups_api

    created = await groups_api.create_group(
        GroupCreate(
            name=body.name,
            description=body.description,
            is_disabled=body.is_disabled,
            config_tag=body.config_tag,
            usage_cost=body.usage_cost,
            order_kind=body.order_kind,
            notices=body.notices,
            category_id=body.category_id,
            panels=[
                GroupPanelLink(
                    panel_id=p.panel_id, config=p.config, priority=p.priority
                )
                for p in body.panels
            ],
        ),
        db=db,
        _admin=_admin,
    )
    return _group_to_integration(created)


@router.put(
    "/api/v1/groups/{group_id}",
    response_model=IntegrationGroupOut,
    summary="ویرایش گروه",
)
async def v1_update_group(
    group_id: int,
    body: IntegrationGroupUpdate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    from app.api import groups as groups_api

    updated = await groups_api.update_group(
        group_id,
        GroupUpdate(
            name=body.name,
            description=body.description,
            is_disabled=body.is_disabled,
            config_tag=body.config_tag,
            usage_cost=body.usage_cost,
            clear_usage_cost=body.clear_usage_cost,
            order_kind=body.order_kind,
            notices=body.notices,
            category_id=body.category_id,
            clear_category=body.clear_category,
            panels=(
                [
                    GroupPanelLink(
                        panel_id=p.panel_id, config=p.config, priority=p.priority
                    )
                    for p in body.panels
                ]
                if body.panels is not None
                else None
            ),
        ),
        db=db,
        _admin=_admin,
    )
    return _group_to_integration(updated)


@router.delete(
    "/api/v1/groups/{group_id}",
    summary="حذف گروه",
    description="گروه را حذف می‌کند و ریموت‌های مرتبط را از پنل‌ها قطع می‌کند.",
)
async def v1_delete_group(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    from app.api import groups as groups_api

    return await groups_api.delete_group(group_id, db=db, _admin=_admin)


@router.get(
    "/api/v1/panel-types",
    response_model=list[PanelTypeInfo],
    summary="لیست تایپ‌های مجاز هنگام ثبت پنل",
    description=(
        "قبل از `POST /api/admin/panels` این را بخوانید. "
        "مقدار `type` را **دقیقاً** یکی از این رشته‌ها بگذارید "
        "(مثلاً `sanaei` نه `Sanaei` / `3x-ui`)."
    ),
)
async def v1_panel_types(_admin=Depends(get_current_admin)):
    """Return canonical PanelType values for integrators registering panels."""
    return list(_PANEL_TYPE_DOCS)


@router.get(
    "/api/v1/panels/sub-status",
    response_model=PanelsSubStatusResponse,
    summary="نتیجه تست و رتبه ساب هر پنل",
    description=(
        "برای هر پنل: کانفیگ‌هایش در ترتیب سراسری ساب، رتبه (`sub_rank`)، "
        "میانگین پینگ تست چنددستگاهی، و خلاصه بهترین/بدترین پینگ.\n\n"
        "اختیاری: `?panel_id=12` فقط همان پنل."
    ),
)
async def v1_panels_sub_status(
    panel_id: int | None = Query(None, description="فیلتر یک پنل"),
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    return await sub_status_svc.panels_sub_status(db, panel_id=panel_id)


@router.get(
    "/api/v1/panels/{panel_id}/sub-status",
    response_model=PanelSubStatusOut,
    summary="تست و رتبه ساب یک پنل",
)
async def v1_one_panel_sub_status(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    res = await sub_status_svc.panels_sub_status(db, panel_id=panel_id)
    if not res.panels:
        raise HTTPException(status_code=404, detail="پنل پیدا نشد")
    return res.panels[0]
