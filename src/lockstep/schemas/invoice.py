import uuid
from datetime import date

from pydantic import BaseModel

ACTION_BY_RISK_TIER: dict[str, str] = {
    "PENDING": "Processing",
    "SAFE": "No action needed",
    "LOW_RISK": "Ask vendor to correct and refile",
    "HIGH_RISK": "Send reminder to vendor before the deadline",
    "BLOCKED": "Write off — not claimable",
}


class InvoiceOut(BaseModel):
    id: uuid.UUID
    invoice_number: str
    status: str
    risk_tier: str
    action: str
    ai_summary: str | None
    citations: list[str] | None
    recoverable_until: date | None
    itc_amount: float | None
    vendor_name: str | None
    raw_data: dict

    @classmethod
    def build(cls, invoice, vendor_name: str | None) -> "InvoiceOut":
        status_value = str(invoice.status)
        risk_tier_value = str(invoice.risk_tier)
        return cls(
            id=invoice.id,
            invoice_number=invoice.invoice_number,
            status=status_value,
            risk_tier=risk_tier_value,
            action=ACTION_BY_RISK_TIER.get(risk_tier_value, "Review"),
            ai_summary=invoice.ai_summary,
            citations=invoice.citations,
            recoverable_until=invoice.recoverable_until,
            itc_amount=float(invoice.itc_amount) if invoice.itc_amount is not None else None,
            vendor_name=vendor_name,
            raw_data=invoice.raw_data,
        )
