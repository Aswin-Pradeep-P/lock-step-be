import csv
import io
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.api.deps import get_current_user
from lockstep.database import get_db
from lockstep.models.enums import InvoiceMatchStatus, InvoiceSource
from lockstep.models.invoice import Invoice
from lockstep.models.user import User
from lockstep.models.vendor import Vendor
from lockstep.schemas.invoice import InvoiceOut

router = APIRouter(prefix="/periods/{period_id}/invoices", tags=["invoices"])


def _parse_status(value: str | None) -> InvoiceMatchStatus | None:
    if not value:
        return None
    try:
        return InvoiceMatchStatus(value.upper())
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown status: {value}") from None


async def _load(
    db: AsyncSession,
    period_id: uuid.UUID,
    status_filter: str | None,
    vendor_id: uuid.UUID | None,
    limit: int,
    offset: int,
) -> list[Invoice]:
    query = select(Invoice).where(Invoice.period_id == period_id)
    parsed = _parse_status(status_filter)
    if parsed:
        query = query.where(Invoice.status == parsed)
    if vendor_id:
        query = query.where(Invoice.vendor_id == vendor_id)
    # One row per matched pair: keep the ledger side and carry the 2B side as
    # `counterpart`, so a pair is not listed twice. Unmatched 2B rows still show.
    query = query.where(
        ~(
            (Invoice.source == InvoiceSource.GSTR2B.value)
            & Invoice.matched_invoice_id.isnot(None)
        )
    )
    return (
        await db.execute(query.order_by(Invoice.status, Invoice.invoice_number)
                         .limit(limit).offset(offset))
    ).scalars().all()


async def _decorate(db: AsyncSession, invoices: list[Invoice]) -> list[InvoiceOut]:
    vendor_ids = {i.vendor_id for i in invoices if i.vendor_id}
    names = {}
    if vendor_ids:
        names = {
            v.id: v.name
            for v in (
                await db.execute(select(Vendor).where(Vendor.id.in_(vendor_ids)))
            ).scalars().all()
        }

    counterpart_ids = {i.matched_invoice_id for i in invoices if i.matched_invoice_id}
    counterparts: dict[uuid.UUID, Invoice] = {}
    if counterpart_ids:
        counterparts = {
            c.id: c
            for c in (
                await db.execute(select(Invoice).where(Invoice.id.in_(counterpart_ids)))
            ).scalars().all()
        }

    return [
        InvoiceOut.build(
            inv,
            names.get(inv.vendor_id),
            counterparts.get(inv.matched_invoice_id) if inv.matched_invoice_id else None,
        )
        for inv in invoices
    ]


@router.get("", response_model=list[InvoiceOut])
async def list_invoices(
    period_id: uuid.UUID,
    status_filter: str | None = Query(None, alias="status"),
    vendor_id: uuid.UUID | None = Query(None),
    limit: int = Query(500, le=5000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    invoices = await _load(db, period_id, status_filter, vendor_id, limit, offset)
    return await _decorate(db, invoices)


@router.get("/export")
async def export_invoices(
    period_id: uuid.UUID,
    status_filter: str | None = Query(None, alias="status"),
    vendor_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CSV export honouring the active filters."""
    invoices = await _load(db, period_id, status_filter, vendor_id, 100_000, 0)
    rows = await _decorate(db, invoices)

    fieldnames = [
        "invoice_number", "invoice_date", "vendor_name", "vendor_gstin", "source",
        "taxable_value", "igst", "cgst", "sgst", "cess", "total_tax",
        "status", "match_reason", "itc_available", "itc_reason", "is_reverse_charge",
        "supplier_filed_at", "carried_from_period",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: getattr(row, k, "") for k in fieldnames})

    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="period_{period_id}_invoices.csv"'
        },
    )
