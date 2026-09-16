"""Write path for `vendor_filing_history`.

Every 2B upload is an observation: this vendor filed, on this date, this many invoices.
A vendor in the ledger but absent from 2B is also an observation — they have *not* filed.
Both are recorded, because the absence is what the product acts on.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.models.vendor import Vendor
from lockstep.models.vendor_filing_history import VendorFilingHistory
from lockstep.services.ingestion import CanonicalRow
from lockstep.services.periods import days_past_cutoff


async def record_observations(
    db: AsyncSession,
    tax_period: str,
    gstr2b_rows: list[CanonicalRow],
    ledger_gstins: set[str] | None = None,
) -> int:
    """Upsert one row per vendor for this period. Returns rows touched.

    Re-running a check re-observes: a vendor who filed since the last upload flips from
    not-filed to filed, which is exactly the delta the period view shows.
    """
    filed: dict[str, dict] = {}
    for row in gstr2b_rows:
        if not row.gstin_normalized:
            continue
        entry = filed.setdefault(
            row.gstin_normalized, {"filed_at": row.supplier_filed_at, "count": 0}
        )
        entry["count"] += 1
        # Keep the earliest filing date seen for the vendor this period.
        if row.supplier_filed_at and (
            entry["filed_at"] is None or row.supplier_filed_at < entry["filed_at"]
        ):
            entry["filed_at"] = row.supplier_filed_at

    not_filed = {g for g in (ledger_gstins or set()) if g and g not in filed}
    gstins = set(filed) | not_filed
    if not gstins:
        return 0

    vendors = (
        await db.execute(select(Vendor).where(Vendor.gstin.in_(gstins)))
    ).scalars().all()
    vendor_by_gstin = {v.gstin: v for v in vendors}

    existing = (
        await db.execute(
            select(VendorFilingHistory).where(
                VendorFilingHistory.tax_period == tax_period,
                VendorFilingHistory.vendor_id.in_([v.id for v in vendors]),
            )
        )
    ).scalars().all()
    existing_by_vendor = {row.vendor_id: row for row in existing}

    touched = 0
    for gstin in gstins:
        vendor = vendor_by_gstin.get(gstin)
        if vendor is None:
            continue  # unmapped vendor — the invoice keeps the GSTIN, history needs a vendor
        entry = filed.get(gstin)
        filed_at = entry["filed_at"] if entry else None
        record = existing_by_vendor.get(vendor.id)
        if record is None:
            record = VendorFilingHistory(vendor_id=vendor.id, tax_period=tax_period)
            db.add(record)
        record.gstr1_filed = entry is not None
        record.gstr1_filed_at = filed_at
        record.days_past_cutoff = days_past_cutoff(tax_period, filed_at)
        record.invoice_count = entry["count"] if entry else 0
        touched += 1

    return touched
