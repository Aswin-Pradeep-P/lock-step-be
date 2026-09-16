import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.api.deps import get_current_user
from lockstep.config import get_settings
from lockstep.database import get_db
from lockstep.models.enums import ActionType
from lockstep.models.invoice import Invoice
from lockstep.models.invoice_action import InvoiceAction
from lockstep.models.period import ReconciliationPeriod
from lockstep.models.user import User
from lockstep.schemas.action import (
    ActionCreate,
    ActionOut,
    ActionProposalOut,
    ThresholdsOut,
)
from lockstep.services import actions as action_service
from lockstep.services.ai_summaries import draft_vendor_email
from lockstep.services.vendor_scoring import get_vendor_risk

router = APIRouter(tags=["actions"])


async def _require_invoice(db: AsyncSession, invoice_id: uuid.UUID) -> Invoice:
    invoice = await db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    return invoice


@router.get("/thresholds", response_model=ThresholdsOut)
async def get_thresholds(current_user: User = Depends(get_current_user)):
    """Auto-notify always; propose a hold above X; require approval above Y."""
    settings = get_settings()
    return ThresholdsOut(
        hold_proposal_threshold=Decimal(str(settings.action_hold_proposal_threshold)),
        approval_threshold=Decimal(str(settings.action_approval_threshold)),
    )


@router.put("/thresholds", response_model=ThresholdsOut)
async def set_thresholds(
    body: ThresholdsOut, current_user: User = Depends(get_current_user)
):
    """Editable live — a judge should be able to move these mid-demo.

    ponytail: process-local, so it resets on restart. Persist per-org when auth is real.
    """
    settings = get_settings()
    settings.action_hold_proposal_threshold = float(body.hold_proposal_threshold)
    settings.action_approval_threshold = float(body.approval_threshold)
    return body


@router.get("/invoices/{invoice_id}/proposal", response_model=ActionProposalOut)
async def propose_action(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    invoice = await _require_invoice(db, invoice_id)
    proposal = action_service.propose(invoice.total_tax)
    return ActionProposalOut(
        invoice_id=invoice.id,
        amount_at_risk=invoice.total_tax,
        proposed_action=str(proposal.proposed_action),
        requires_approval=proposal.requires_approval,
        rationale=proposal.rationale,
    )


@router.get("/invoices/{invoice_id}/actions", response_model=list[ActionOut])
async def list_actions(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _require_invoice(db, invoice_id)
    entries = (
        await db.execute(
            select(InvoiceAction)
            .where(InvoiceAction.invoice_id == invoice_id)
            .order_by(InvoiceAction.created_at.desc())
        )
    ).scalars().all()
    return [ActionOut.model_validate(e) for e in entries]


@router.post(
    "/invoices/{invoice_id}/actions", response_model=ActionOut, status_code=status.HTTP_201_CREATED
)
async def create_action(
    invoice_id: uuid.UUID,
    body: ActionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Append to the audit trail. Never updates an earlier entry."""
    invoice = await _require_invoice(db, invoice_id)
    entry = await action_service.record(
        db, invoice, body.action,
        user_id=current_user.id, channel=body.channel, payload=body.payload,
    )
    if body.action == ActionType.MARKED_RESOLVED:
        from lockstep.models.enums import InvoiceMatchStatus

        invoice.status = InvoiceMatchStatus.RESOLVED
    await db.commit()
    await db.refresh(entry)
    return ActionOut.model_validate(entry)


@router.get("/vendors/{vendor_id}/email-draft")
async def vendor_email_draft(
    vendor_id: uuid.UUID,
    period_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate the notification draft. We store it and log the action; we never send."""
    period = await db.get(ReconciliationPeriod, period_id)
    if period is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Period not found")

    vendor = next(
        (
            v for v in await get_vendor_risk(db, period_id=period.id, tax_period=period.tax_period)
            if v.vendor_id == str(vendor_id)
        ),
        None,
    )
    if vendor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vendor not found")

    return await draft_vendor_email(db, vendor, period.id, period.tax_period)
