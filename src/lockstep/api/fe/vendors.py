"""GET /api/vendors — returns FE-shaped vendor list with risk scoring."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.database import get_db
from lockstep.models.enums import InvoiceMatchStatus
from lockstep.models.invoice import Invoice
from lockstep.models.vendor import Vendor
from lockstep.schemas.fe_types import VendorOut

router = APIRouter(prefix="/vendors", tags=["fe-vendors"])


def _compute_risk_tier(missing: int, total: int) -> str:
    if total == 0:
        return "green"
    ratio = missing / total
    if ratio >= 0.3 or missing >= 3:
        return "red"
    if ratio >= 0.1 or missing >= 1:
        return "amber"
    return "green"


def _compute_risk_score(missing: int, clerical: int, total: int) -> float:
    if total == 0:
        return 0
    return round(min(100, (missing * 20 + clerical * 5) / max(total, 1) * 100), 1)


@router.get("")
async def list_vendors(db: AsyncSession = Depends(get_db)) -> JSONResponse:
    vendors_result = await db.execute(select(Vendor).where(Vendor.is_active == True))
    vendors = vendors_result.scalars().all()

    if not vendors:
        return JSONResponse(content=[])

    stats_query = (
        select(
            Invoice.vendor_id,
            Invoice.status,
            func.count().label("cnt"),
        )
        .where(Invoice.vendor_id.is_not(None))
        .group_by(Invoice.vendor_id, Invoice.status)
    )
    stats_result = await db.execute(stats_query)

    by_vendor: dict[str, dict] = {}
    for vendor_id, inv_status, cnt in stats_result.all():
        vid = str(vendor_id)
        entry = by_vendor.setdefault(vid, {"total": 0, "exact": 0, "clerical": 0, "missing": 0})
        entry["total"] += cnt
        status_str = str(inv_status)
        if status_str == InvoiceMatchStatus.EXACT_MATCH:
            entry["exact"] += cnt
        elif status_str == InvoiceMatchStatus.CLERICAL_MISMATCH:
            entry["clerical"] += cnt
        elif status_str == InvoiceMatchStatus.MISSING_IN_GSTR2B:
            entry["missing"] += cnt

    result = []
    for vendor in vendors:
        vid = str(vendor.id)
        stats = by_vendor.get(vid, {"total": 0, "exact": 0, "clerical": 0, "missing": 0})
        total = stats["total"]
        missing = stats["missing"]
        clerical = stats["clerical"]

        result.append(VendorOut(
            id=vid,
            name=vendor.name,
            gstin=vendor.gstin,
            risk_score=_compute_risk_score(missing, clerical, total),
            risk_tier=_compute_risk_tier(missing, total),
            last_filing_date=vendor.updated_at.strftime("%Y-%m-%d") if vendor.updated_at else "",
            total_invoices=total,
            missed_filings=missing,
        ))

    result.sort(key=lambda v: -v.risk_score)
    return JSONResponse(content=[v.to_camel_dict() for v in result])
