"""Dashboard aggregates. The headline is a countdown and an exposure, never a count.

    ₹4.2L at risk · 6 days to the 13th · 11 vendors haven't filed
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.models.enums import AT_RISK_STATUSES, InvoiceMatchStatus, InvoiceSource
from lockstep.models.invoice import Invoice
from lockstep.models.period import ReconciliationCheck, ReconciliationPeriod
from lockstep.services.periods import days_to_cutoff
from lockstep.services.vendor_scoring import VendorRiskRow, get_vendor_risk

_TAX_SUM = func.coalesce(func.sum(Invoice.igst + Invoice.cgst + Invoice.sgst + Invoice.cess), 0)


async def latest_check_id(db: AsyncSession, period_id):
    """A period holds many checks. Every "what is true now" read uses the newest one —
    earlier checks are history, not additional invoices."""
    return (
        await db.execute(
            select(ReconciliationCheck.id)
            .where(ReconciliationCheck.period_id == period_id)
            .order_by(ReconciliationCheck.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


@dataclass
class PeriodHeadline:
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


async def status_counts(db: AsyncSession, period_id, check_id=None) -> dict[str, int]:
    check_id = check_id or await latest_check_id(db, period_id)
    rows = (
        await db.execute(
            select(Invoice.status, func.count())
            .where(
                Invoice.period_id == period_id,
                Invoice.check_id == check_id,
                # A matched pair is stored as two rows pointing at each other (the
                # ledger side and the 2B side), so counting both double-counts every
                # match: 50 exact matches read as 100. Count the ledger side, and the
                # 2B-only rows that have no ledger counterpart. `period_headline`'s
                # exposure query already guards this the same way.
                ~(
                    (Invoice.source == InvoiceSource.GSTR2B.value)
                    & Invoice.matched_invoice_id.isnot(None)
                ),
            )
            .group_by(Invoice.status)
        )
    ).all()
    counts = {str(status): 0 for status in InvoiceMatchStatus}
    for status, count in rows:
        counts[str(status)] = count
    return counts


async def period_headline(
    db: AsyncSession, period: ReconciliationPeriod, vendors: list[VendorRiskRow] | None = None
) -> PeriodHeadline:
    check_id = await latest_check_id(db, period.id)
    at_risk = (
        await db.execute(
            select(func.count(), _TAX_SUM).where(
                Invoice.period_id == period.id,
                Invoice.check_id == check_id,
                Invoice.status.in_(AT_RISK_STATUSES),
                # A matched pair writes both sides; count the ledger side only so
                # exposure is not doubled.
                Invoice.source != "GSTR2B",
            )
        )
    ).one()

    checks = (
        await db.execute(
            select(func.count()).select_from(ReconciliationCheck).where(
                ReconciliationCheck.period_id == period.id
            )
        )
    ).scalar_one()

    vendors = vendors if vendors is not None else await get_vendor_risk(
        db, period_id=period.id, tax_period=period.tax_period, check_id=check_id
    )
    days = days_to_cutoff(period.tax_period)

    return PeriodHeadline(
        period_id=str(period.id),
        tax_period=period.tax_period,
        cutoff_date=period.cutoff_date.isoformat(),
        days_to_cutoff=days,
        window_open=days >= 0,
        amount_at_risk=Decimal(at_risk[1]),
        invoices_at_risk=at_risk[0],
        vendors_not_filed=sum(
            1 for v in vendors if not v.filed_this_period and v.current_exposure > 0
        ),
        checks_run=checks,
        status_counts=await status_counts(db, period.id, check_id),
    )


async def check_delta(db: AsyncSession, period: ReconciliationPeriod) -> dict:
    """What changed between the last two checks — *three vendors filed since Tuesday*.

    A one-shot run cannot express this; it is the reason a period holds many checks.
    """
    checks = (
        await db.execute(
            select(ReconciliationCheck)
            .where(ReconciliationCheck.period_id == period.id)
            .order_by(ReconciliationCheck.created_at.desc())
            .limit(2)
        )
    ).scalars().all()
    if len(checks) < 2:
        return {"has_previous": False}

    async def missing_for(check_id) -> tuple[int, Decimal]:
        row = (
            await db.execute(
                select(func.count(), _TAX_SUM).where(
                    Invoice.check_id == check_id,
                    Invoice.status == InvoiceMatchStatus.MISSING_IN_GSTR2B,
                )
            )
        ).one()
        return row[0], Decimal(row[1])

    latest_count, latest_amount = await missing_for(checks[0].id)
    previous_count, previous_amount = await missing_for(checks[1].id)

    return {
        "has_previous": True,
        "since": checks[1].created_at.isoformat(),
        "invoices_resolved": previous_count - latest_count,
        "amount_recovered": previous_amount - latest_amount,
        "still_missing": latest_count,
        "still_at_risk": latest_amount,
    }
