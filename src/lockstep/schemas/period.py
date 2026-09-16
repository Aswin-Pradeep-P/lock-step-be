import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator

from lockstep.services.periods import parse_tax_period


class ClientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    legal_name: str
    gstin: str


class ClientCreate(BaseModel):
    legal_name: str
    gstin: str


class PeriodCreate(BaseModel):
    client_id: uuid.UUID
    tax_period: str  # MMYYYY

    @field_validator("tax_period")
    @classmethod
    def _valid_period(cls, value: str) -> str:
        parse_tax_period(value)  # raises ValueError -> 422
        return value


class PeriodOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    client_id: uuid.UUID
    tax_period: str
    cutoff_date: date
    gstr2b_date: date
    filing_due: date
    created_at: datetime


class CheckOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    period_id: uuid.UUID
    status: str
    rows_parsed: int | None
    error_message: str | None
    column_mapping: dict | None
    created_at: datetime


class HeadlineOut(BaseModel):
    """The only numbers that go above the fold."""

    period_id: str
    tax_period: str
    cutoff_date: str
    days_to_cutoff: int
    window_open: bool
    amount_at_risk: Decimal
    invoices_at_risk: int
    vendors_not_filed: int
    checks_run: int
    status_counts: dict[str, int]
