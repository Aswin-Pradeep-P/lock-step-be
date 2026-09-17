import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.api.deps import get_current_user
from lockstep.config import get_settings
from lockstep.database import get_db
from lockstep.models.enums import AT_RISK_STATUSES, ActionType
from lockstep.models.invoice import Invoice
from lockstep.models.invoice_action import InvoiceAction
from lockstep.models.period import ReconciliationPeriod
from lockstep.models.user import User
from lockstep.models.vendor import Vendor
from lockstep.schemas.action import (
    ActionCreate,
    ActionOut,
    ActionProposalOut,
    BulkNudgeOut,
    BulkNudgeRequest,
    InvoiceInsightOut,
    ThresholdsOut,
)
from lockstep.services import actions as action_service
from lockstep.services.ai_summaries import draft_vendor_email
from lockstep.services.dashboard import latest_check_id
from lockstep.services.invoice_insight import generate_insight
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


@router.get("/invoices/{invoice_id}/insight", response_model=InvoiceInsightOut)
async def invoice_insight(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """AI-authored reason + suggested next step, generated once and cached on the
    row. A second, clearly-labelled layer over `match_reason` — never a
    replacement for it; the deterministic reason is unaffected either way."""
    invoice = await _require_invoice(db, invoice_id)
    if invoice.ai_reason_md and invoice.ai_suggestion_md:
        return InvoiceInsightOut(
            reason_md=invoice.ai_reason_md, suggestion_md=invoice.ai_suggestion_md
        )

    vendor_name = None
    if invoice.vendor_id:
        vendor = await db.get(Vendor, invoice.vendor_id)
        vendor_name = vendor.name if vendor else None

    insight = await generate_insight(invoice, vendor_name)
    invoice.ai_reason_md = insight.reason_md
    invoice.ai_suggestion_md = insight.suggestion_md
    await db.commit()
    return InvoiceInsightOut(reason_md=insight.reason_md, suggestion_md=insight.suggestion_md)


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


@router.post(
    "/periods/{period_id}/actions/bulk-nudge",
    response_model=BulkNudgeOut,
    status_code=status.HTTP_201_CREATED,
)
async def bulk_nudge(
    period_id: uuid.UUID,
    body: BulkNudgeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Record VENDOR_NOTIFIED on every at-risk invoice for the selected vendors.

    Mock send only — same as single-invoice nudge. One commit for the whole batch.
    """
    period = await db.get(ReconciliationPeriod, period_id)
    if period is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Period not found")

    if not body.vendor_ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "vendor_ids required")

    check_id = body.check_id or await latest_check_id(db, period_id)
    if check_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No checks for this period")

    invoices = (
        await db.execute(
            select(Invoice).where(
                Invoice.period_id == period_id,
                Invoice.check_id == check_id,
                Invoice.vendor_id.in_(body.vendor_ids),
                Invoice.status.in_(AT_RISK_STATUSES),
                Invoice.source != "GSTR2B",
            )
        )
    ).scalars().all()

    action_ids: list[uuid.UUID] = []
    nudged_vendor_ids: set[uuid.UUID] = set()
    channel = body.channel or "email"

    for invoice in invoices:
        entry = await action_service.record(
            db,
            invoice,
            ActionType.VENDOR_NOTIFIED,
            user_id=current_user.id,
            channel=channel,
            payload={"bulk": True, "vendor_id": str(invoice.vendor_id)},
        )
        await db.flush()
        action_ids.append(entry.id)
        if invoice.vendor_id is not None:
            nudged_vendor_ids.add(invoice.vendor_id)

    await db.commit()
    return BulkNudgeOut(
        nudged_vendors=len(nudged_vendor_ids),
        nudged_invoices=len(action_ids),
        action_ids=action_ids,
    )


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
