from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin
from app.core.database import get_db
from app.models.template import UserTemplate
from app.schemas.template import TemplateCreate, TemplateOut, TemplateUpdate

router = APIRouter(tags=["User Template"])


@router.post("/api/user_template", response_model=TemplateOut)
async def create_template(
    body: TemplateCreate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    tpl = UserTemplate(**body.model_dump())
    db.add(tpl)
    await db.commit()
    await db.refresh(tpl)
    return TemplateOut.model_validate(tpl)


@router.get("/api/user_template", response_model=list[TemplateOut])
async def list_templates(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    result = await db.execute(select(UserTemplate).order_by(UserTemplate.id))
    return [TemplateOut.model_validate(t) for t in result.scalars().all()]


@router.get("/api/user_template/{template_id}", response_model=TemplateOut)
async def get_template(
    template_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    tpl = await db.get(UserTemplate, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    return TemplateOut.model_validate(tpl)


@router.put("/api/user_template/{template_id}", response_model=TemplateOut)
async def update_template(
    template_id: int,
    body: TemplateUpdate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    tpl = await db.get(UserTemplate, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(tpl, k, v)
    await db.commit()
    await db.refresh(tpl)
    return TemplateOut.model_validate(tpl)


@router.delete("/api/user_template/{template_id}")
async def delete_template(
    template_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    tpl = await db.get(UserTemplate, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    await db.delete(tpl)
    await db.commit()
    return {"detail": "Template deleted"}
