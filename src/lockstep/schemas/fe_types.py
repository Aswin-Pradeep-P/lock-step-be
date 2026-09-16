"""Pydantic schemas that match the frontend TypeScript types exactly.

Every schema uses camelCase aliases so JSON serialization matches what the
React frontend expects (e.g. ``invoiceNo``, ``aiSummary``, ``matchConfidence``).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )

    def to_camel_dict(self) -> dict:
        return self.model_dump(by_alias=True, mode="json")


# ── Input record types (what the FE sends) ──────────────────────────────────

class PurchaseRecordIn(CamelModel):
    date: str = ""
    particulars: str = ""
    supplier: str = ""
    voucher_type: str = ""
    voucher_no: str = ""
    voucher_ref_no: str = ""
    voucher_ref_date: str = ""
    narration: str = ""
    gross_total: float = 0
    igst_input: float | None = 0
    cgst_input: float | None = 0
    sgst_input: float | None = 0


class GSTR2BRecordIn(CamelModel):
    gstin: str = ""
    trade_name: str = ""
    invoice_no: str = ""
    invoice_type: str = ""
    invoice_date: str = ""
    invoice_value: float = 0
    place_of_supply: str = ""
    reverse_charge: str = ""
    taxable_value: float = 0
    igst: float = 0
    cgst: float = 0
    sgst: float = 0
    cess: float = 0
    filing_period: str = ""
    filing_date: str = ""
    itc_availability: str = ""
    reason: str = ""
    applicable_tax_rate: str = ""
    source: str = ""
    irn: str = ""
    irn_date: str = ""


# ── Request bodies ───────────────────────────────────────────────────────────

class ReconcileRequest(CamelModel):
    purchase_records: list[PurchaseRecordIn]
    gstr2b_records: list[GSTR2BRecordIn] = Field(alias="gstr2bRecords")
    purchase_file_name: str
    gstr2b_file_name: str = Field(alias="gstr2bFileName")


class ActionRequest(CamelModel):
    action: Literal["flagged", "escalated", "resolved"]


class NudgeRequest(CamelModel):
    channel: Literal["email", "whatsapp"]
    message: str


class TallyConnectRequest(CamelModel):
    host: str = "localhost"
    port: int = 9000


class TallyPurchaseRequest(CamelModel):
    host: str = "localhost"
    port: int = 9000
    company: str
    from_date: str
    to_date: str


# ── Response types ───────────────────────────────────────────────────────────

SuggestionAction = Literal[
    "auto_correct", "nudge_vendor", "switch_vendor", "accept_risk", "escalate_urgent"
]
RiskCategory = Literal["matched", "low_risk", "high_risk", "cannot_file"]
ActionStatus = Literal["none", "flagged", "escalated", "resolved"]
NudgeChannel = Literal["email", "whatsapp"]
ActivityType = Literal["created", "flagged", "escalated", "resolved", "nudge_sent"]


class AiSuggestion(CamelModel):
    id: str
    action: SuggestionAction
    label: str
    description: str
    confidence: float


class ActivityEntry(CamelModel):
    id: str
    timestamp: str
    type: ActivityType
    description: str
    actor: str
    channel: NudgeChannel | None = None


class ReconciledRecord(CamelModel):
    id: str
    invoice_no: str
    invoice_date: str
    supplier_name: str
    gstin: str
    taxable_value: float
    igst: float
    cgst: float
    sgst: float
    total_tax: float
    status: RiskCategory
    match_confidence: float
    ai_summary: str
    ai_suggestions: list[AiSuggestion] = []
    action_status: ActionStatus = "none"
    activity_log: list[ActivityEntry] = []
    purchase_record: PurchaseRecordIn | None = None
    gstr2b_record: GSTR2BRecordIn | None = Field(default=None, alias="gstr2bRecord")


class ReconciliationRunOut(CamelModel):
    id: str
    created_at: str
    purchase_file_name: str
    gstr2b_file_name: str = Field(alias="gstr2bFileName")
    total_records: int
    matched_count: int
    low_risk_count: int
    high_risk_count: int
    cannot_file_count: int
    total_taxable_value: float
    total_tax_at_risk: float
    records: list[ReconciledRecord]


class ReconciliationRunSummary(CamelModel):
    id: str
    created_at: str
    purchase_file_name: str
    gstr2b_file_name: str = Field(alias="gstr2bFileName")
    total_records: int
    matched_count: int
    low_risk_count: int
    high_risk_count: int
    cannot_file_count: int
    total_taxable_value: float
    total_tax_at_risk: float


class VendorOut(CamelModel):
    id: str
    name: str
    gstin: str
    risk_score: float
    risk_tier: Literal["green", "amber", "red"]
    last_filing_date: str
    total_invoices: int
    missed_filings: int


class TallyConnectResponse(CamelModel):
    connected: bool
    companies: list[str] = []
    warning: str | None = None
    error: str | None = None


class TallyPurchaseResponse(CamelModel):
    records: list[PurchaseRecordIn]
    count: int
    company: str
    period: dict
