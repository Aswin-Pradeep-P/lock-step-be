import uuid
from datetime import datetime

from pydantic import BaseModel

from lockstep.models.enums import InvoiceRiskTier


class RunCreateResponse(BaseModel):
    id: uuid.UUID
    status: str


class RiskTierCounts(BaseModel):
    pending: int = 0
    safe: int = 0
    low_risk: int = 0
    high_risk: int = 0
    blocked: int = 0

    @classmethod
    def from_counts(cls, counts: dict[InvoiceRiskTier, int]) -> "RiskTierCounts":
        return cls(
            pending=counts.get(InvoiceRiskTier.PENDING, 0),
            safe=counts.get(InvoiceRiskTier.SAFE, 0),
            low_risk=counts.get(InvoiceRiskTier.LOW_RISK, 0),
            high_risk=counts.get(InvoiceRiskTier.HIGH_RISK, 0),
            blocked=counts.get(InvoiceRiskTier.BLOCKED, 0),
        )


class RunOut(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    error_message: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class RunDetailOut(RunOut):
    bucket_counts: RiskTierCounts
