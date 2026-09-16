import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from lockstep.models.enums import ActionType


class ActionCreate(BaseModel):
    action: ActionType
    channel: str | None = None
    payload: dict | None = None


class ActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    invoice_id: uuid.UUID
    action: str
    channel: str | None
    auto_proposed: bool
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    amount_at_risk: Decimal | None
    payload: dict | None
    created_at: datetime


class ThresholdsOut(BaseModel):
    """Editable live — a judge will want to move these during the demo."""

    hold_proposal_threshold: Decimal
    approval_threshold: Decimal


class ActionProposalOut(BaseModel):
    invoice_id: uuid.UUID
    amount_at_risk: Decimal
    proposed_action: str
    requires_approval: bool
    rationale: str
