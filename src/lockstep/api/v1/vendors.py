from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.api.deps import get_current_user
from lockstep.database import get_db
from lockstep.models.user import User
from lockstep.schemas.vendor import VendorRiskOut
from lockstep.services.vendor_scoring import get_vendor_risk_matrix

router = APIRouter(prefix="/vendors", tags=["vendors"])


@router.get("", response_model=list[VendorRiskOut])
async def list_vendor_risk(
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
) -> list[VendorRiskOut]:
    rows = await get_vendor_risk_matrix(db)
    return [VendorRiskOut(**vars(r)) for r in rows]
