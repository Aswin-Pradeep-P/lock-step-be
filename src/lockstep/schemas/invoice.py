import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from lockstep.models.invoice import Invoice
from lockstep.services.risk_rules import days_remaining, is_window_open


class InvoiceSideOut(BaseModel):
    """One side of a mismatch, for the side-by-side diff."""

    invoice_number: str
    invoice_date: date | None
    taxable_value: Decimal | None
    igst: Decimal
    cgst: Decimal
    sgst: Decimal
    cess: Decimal
    total_tax: Decimal
    raw_data: dict


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    period_id: uuid.UUID
    check_id: uuid.UUID
    vendor_id: uuid.UUID | None
    vendor_gstin: str | None
    vendor_name: str | None
    source: str
    status: str
    match_reason: str | None
    carried_from_period: str | None

    invoice_number: str
    invoice_date: date | None
    taxable_value: Decimal | None
    igst: Decimal
    cgst: Decimal
    sgst: Decimal
    cess: Decimal
    total_tax: Decimal

    itc_available: bool | None
    itc_reason: str | None
    is_reverse_charge: bool
    supplier_filed_at: date | None
    description: str | None

    # Populated lazily, on first `GET /invoices/{id}/insight` — included here so a
    # page that reloads an already-reviewed invoice doesn't have to re-fetch it.
    ai_reason_md: str | None
    ai_suggestion_md: str | None

    # Sec 16(4): the date is stored on every invoice, but until now nothing ever
    # compared it to today — computed here, at read time, so it can't go stale the
    # way persisting a boolean at match time would.
    recoverable_until: date | None
    days_to_recover: int | None
    window_open: bool | None

    counterpart: InvoiceSideOut | None = None
    created_at: datetime

    @classmethod
    def build(
        cls, invoice: Invoice, vendor_name: str | None = None, counterpart: Invoice | None = None
    ) -> "InvoiceOut":
        deadline = invoice.recoverable_until
        return cls(
            id=invoice.id,
            period_id=invoice.period_id,
            check_id=invoice.check_id,
            vendor_id=invoice.vendor_id,
            vendor_gstin=invoice.vendor_gstin,
            vendor_name=vendor_name,
            source=str(invoice.source),
            status=str(invoice.status),
            match_reason=invoice.match_reason,
            carried_from_period=invoice.carried_from_period,
            invoice_number=invoice.invoice_number,
            invoice_date=invoice.invoice_date,
            taxable_value=invoice.taxable_value,
            igst=invoice.igst,
            cgst=invoice.cgst,
            sgst=invoice.sgst,
            cess=invoice.cess,
            total_tax=invoice.total_tax,
            itc_available=invoice.itc_available,
            itc_reason=invoice.itc_reason,
            is_reverse_charge=invoice.is_reverse_charge,
            supplier_filed_at=invoice.supplier_filed_at,
            description=invoice.description,
            ai_reason_md=invoice.ai_reason_md,
            ai_suggestion_md=invoice.ai_suggestion_md,
            recoverable_until=deadline,
            days_to_recover=days_remaining(deadline) if deadline else None,
            window_open=is_window_open(deadline) if deadline else None,
            counterpart=(
                InvoiceSideOut(
                    invoice_number=counterpart.invoice_number,
                    invoice_date=counterpart.invoice_date,
                    taxable_value=counterpart.taxable_value,
                    igst=counterpart.igst,
                    cgst=counterpart.cgst,
                    sgst=counterpart.sgst,
                    cess=counterpart.cess,
                    total_tax=counterpart.total_tax,
                    raw_data=counterpart.raw_data,
                )
                if counterpart
                else None
            ),
            created_at=invoice.created_at,
        )
