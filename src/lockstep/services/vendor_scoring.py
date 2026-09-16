"""Vendor risk matrix — computed on read from the invoice history, not stored.

Rolling it up at query time (instead of a write-path aggregate table) means
it's always consistent with the underlying invoices and there's nothing to
keep in sync when a run is reprocessed.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.models.enums import InvoiceMatchStatus
from lockstep.models.invoice import Invoice
from lockstep.models.vendor import Vendor

CHRONIC_NON_FILER_THRESHOLD = 3  # missing-in-2B invoices -> "chronic, consider replacing"


@dataclass
class VendorRiskRow:
    vendor_id: str
    name: str
    gstin: str
    total_invoices: int
    exact_count: int
    clerical_count: int
    missing_in_2b_count: int
    at_risk_amount: float
    tier: str


def _tier_for(clerical_count: int, missing_in_2b_count: int) -> str:
    if missing_in_2b_count >= CHRONIC_NON_FILER_THRESHOLD:
        return "CHRONIC"
    if missing_in_2b_count > 0:
        return "HIGH_RISK"
    if clerical_count > 0:
        return "WATCH"
    return "RELIABLE"


async def get_vendor_risk_matrix(db: AsyncSession) -> list[VendorRiskRow]:
    counts_by_status = (
        select(
            Invoice.vendor_id,
            Invoice.status,
            func.count().label("count"),
            func.coalesce(func.sum(Invoice.itc_amount), 0).label("amount"),
        )
        .where(Invoice.vendor_id.is_not(None))
        .group_by(Invoice.vendor_id, Invoice.status)
    )
    rows = (await db.execute(counts_by_status)).all()

    by_vendor: dict[str, dict] = {}
    for vendor_id, status, count, amount in rows:
        entry = by_vendor.setdefault(
            str(vendor_id),
            {"exact": 0, "clerical": 0, "missing_2b": 0, "total": 0, "at_risk_amount": 0.0},
        )
        entry["total"] += count
        if status == InvoiceMatchStatus.EXACT_MATCH:
            entry["exact"] += count
        elif status == InvoiceMatchStatus.CLERICAL_MISMATCH:
            entry["clerical"] += count
        elif status == InvoiceMatchStatus.MISSING_IN_GSTR2B:
            entry["missing_2b"] += count
            entry["at_risk_amount"] += float(amount)

    if not by_vendor:
        return []

    vendors = (
        await db.execute(select(Vendor).where(Vendor.id.in_([k for k in by_vendor])))
    ).scalars().all()

    result = []
    for vendor in vendors:
        stats = by_vendor.get(str(vendor.id))
        if not stats:
            continue
        result.append(
            VendorRiskRow(
                vendor_id=str(vendor.id),
                name=vendor.name,
                gstin=vendor.gstin,
                total_invoices=stats["total"],
                exact_count=stats["exact"],
                clerical_count=stats["clerical"],
                missing_in_2b_count=stats["missing_2b"],
                at_risk_amount=stats["at_risk_amount"],
                tier=_tier_for(stats["clerical"], stats["missing_2b"]),
            )
        )

    result.sort(key=lambda r: (-r.missing_in_2b_count, -r.at_risk_amount))
    return result
