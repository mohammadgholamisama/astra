from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin
from app.core.database import get_db
from app.schemas.config_lookup import ConfigLookupIn, ConfigLookupOut
from app.services import config_lookup as lookup_svc

router = APIRouter(tags=["Config Lookup"])


@router.post("/api/admin/config-lookup", response_model=ConfigLookupOut)
async def lookup_config(
    body: ConfigLookupIn,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    """Paste a share link → which Astra panel + sales group it belongs to."""
    return await lookup_svc.lookup_config(db, body.text)
