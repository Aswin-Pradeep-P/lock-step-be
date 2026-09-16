"""Action proposals and the audit trail.

Thresholds answer the real question — what makes a CA trust an automated hold — by
making it configurable rather than hardcoded: auto-notify always, propose a hold above
X, require a human above Y. Every action is appended to `invoice_actions` with who
approved it; nothing is ever updated in place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.config import get_settings
from lockstep.models.enums import ActionType
from lockstep.models.invoice import Invoice
from lockstep.models.invoice_action import InvoiceAction

#: Actions a human must sign off before they take effect.
APPROVAL_REQUIRED = (ActionType.PAYMENT_HOLD_APPLIED, ActionType.PAYMENT_RELEASED)


@dataclass
class Proposal:
    proposed_action: ActionType
    requires_approval: bool
    rationale: str


def propose(amount_at_risk: Decimal) -> Proposal:
    """Pure threshold rule. Config-driven so the numbers can move mid-demo."""
    settings = get_settings()
    hold_at = Decimal(str(settings.action_hold_proposal_threshold))
    approve_at = Decimal(str(settings.action_approval_threshold))

    if amount_at_risk >= approve_at:
        return Proposal(
            ActionType.PAYMENT_HOLD_PROPOSED, True,
            f"₹{amount_at_risk:,.2f} is above the ₹{approve_at:,.0f} approval threshold — "
            f"a payment hold is proposed and needs sign-off before it is applied.",
        )
    if amount_at_risk >= hold_at:
        return Proposal(
            ActionType.PAYMENT_HOLD_PROPOSED, False,
            f"₹{amount_at_risk:,.2f} is above the ₹{hold_at:,.0f} hold threshold — "
            f"a payment hold is proposed.",
        )
    return Proposal(
        ActionType.VENDOR_NOTIFIED, False,
        f"₹{amount_at_risk:,.2f} is below the ₹{hold_at:,.0f} hold threshold — "
        f"notify the vendor and keep watching.",
    )


async def record(
    db: AsyncSession,
    invoice: Invoice,
    action: ActionType,
    *,
    user_id=None,
    channel: str | None = None,
    payload: dict | None = None,
    auto_proposed: bool = False,
) -> InvoiceAction:
    """Append one entry to the trail. Approval is stamped for actions that need it."""
    needs_approval = action in APPROVAL_REQUIRED
    entry = InvoiceAction(
        invoice_id=invoice.id,
        action=action,
        channel=channel,
        auto_proposed=auto_proposed,
        approved_by=user_id if needs_approval else None,
        approved_at=datetime.now(UTC) if needs_approval else None,
        amount_at_risk=invoice.total_tax,
        payload=payload,
    )
    db.add(entry)
    return entry
