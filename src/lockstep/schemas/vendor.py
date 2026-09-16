import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel


class VendorRiskOut(BaseModel):
    """Risk from filing behaviour, by rules — not a model score."""

    vendor_id: str
    name: str
    gstin: str | None
    gstin_verified: bool
    contact_email: str | None
    periods_observed: int
    on_time_rate: float | None
    avg_days_past_cutoff: float | None
    typical_filing_day: int | None
    filed_this_period: bool
    predicted_late: bool
    missing_invoice_count: int
    current_exposure: Decimal
    risk_band: str


class FilingHistoryOut(BaseModel):
    tax_period: str
    gstr1_filed: bool | None
    gstr1_filed_at: date | None
    days_past_cutoff: int | None
    invoice_count: int | None


class VendorDetailOut(BaseModel):
    id: uuid.UUID
    name: str
    gstin: str | None
    gstin_verified: bool
    contact_email: str | None
    contact_phone: str | None
    risk: VendorRiskOut | None
    filing_history: list[FilingHistoryOut]
    ai_summary: str | None = None
