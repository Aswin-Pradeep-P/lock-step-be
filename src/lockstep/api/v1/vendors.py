import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.api.deps import get_current_user
from lockstep.database import get_db
from lockstep.models.period import ReconciliationPeriod
from lockstep.models.user import User
from lockstep.models.vendor import Vendor
from lockstep.models.vendor_filing_history import VendorFilingHistory
from lockstep.schemas.vendor import FilingHistoryOut, VendorDetailOut, VendorRiskOut
from lockstep.services.ai_summaries import vendor_summary
from lockstep.services.vendor_scoring import get_vendor_risk

router = APIRouter(prefix="/vendors", tags=["vendors"])


async def _period_context(
    db: AsyncSession, period_id: uuid.UUID | None
) -> tuple[uuid.UUID | None, str | None]:
    if period_id is None:
        return None, None
    period = await db.get(ReconciliationPeriod, period_id)
    if period is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Period not found")
    return period.id, period.tax_period


@router.get("", response_model=list[VendorRiskOut])
async def list_vendor_risk(
    period_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Vendors ranked by exposure × risk — how the dashboard orders them."""
    pid, tax_period = await _period_context(db, period_id)
    rows = await get_vendor_risk(db, period_id=pid, tax_period=tax_period)
    return [VendorRiskOut(**{k: v for k, v in vars(r).items()}) for r in rows]


@router.get("/{vendor_id}", response_model=VendorDetailOut)
async def get_vendor(
    vendor_id: uuid.UUID,
    period_id: uuid.UUID | None = Query(None),
    include_summary: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    vendor = await db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vendor not found")

    pid, tax_period = await _period_context(db, period_id)
    risk = next(
        (r for r in await get_vendor_risk(db, period_id=pid, tax_period=tax_period)
         if r.vendor_id == str(vendor_id)),
        None,
    )

    history = (
        await db.execute(
            select(VendorFilingHistory)
            .where(VendorFilingHistory.vendor_id == vendor_id)
            .order_by(VendorFilingHistory.tax_period.desc())
        )
    ).scalars().all()

    summary = None
    if include_summary and risk is not None:
        summary = await vendor_summary(db, risk, tax_period)

    return VendorDetailOut(
        id=vendor.id,
        name=vendor.name,
        gstin=vendor.gstin,
        gstin_verified=vendor.gstin_verified,
        contact_email=vendor.contact_email,
        contact_phone=vendor.contact_phone,
        risk=VendorRiskOut(**vars(risk)) if risk else None,
        filing_history=[FilingHistoryOut.model_validate(h, from_attributes=True) for h in history],
        ai_summary=summary,
    )
