"""Dashboard aggregates. The headline is a countdown and an exposure, never a count.

    ₹4.2L at risk · 6 days to the 13th · 11 vendors haven't filed
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.models.enums import AT_RISK_STATUSES, CheckStatus, InvoiceMatchStatus, InvoiceSource
from lockstep.models.invoice import Invoice
from lockstep.models.period import ReconciliationCheck, ReconciliationPeriod
from lockstep.services.ingestion import normalize_invoice_number
from lockstep.services.periods import days_to_cutoff
from lockstep.services.vendor_scoring import VendorRiskRow, get_vendor_risk

_TAX_SUM = func.coalesce(func.sum(Invoice.igst + Invoice.cgst + Invoice.sgst + Invoice.cess), 0)
_AT_RISK_SET = frozenset(AT_RISK_STATUSES)


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


async def first_completed_check_id(db: AsyncSession, period_id):
    """Earliest completed check of the period. FAILED checks write no rows."""
    return (
        await db.execute(
            select(ReconciliationCheck.id)
            .where(
                ReconciliationCheck.period_id == period_id,
                ReconciliationCheck.status == CheckStatus.COMPLETED.value,
            )
            .order_by(ReconciliationCheck.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _invoice_key(vendor_gstin: str | None, invoice_number: str) -> tuple[str, str]:
    return (vendor_gstin or "", normalize_invoice_number(invoice_number))


async def corrections_for_check(
    db: AsyncSession, period_id, check_id
) -> tuple[int, Decimal]:
    """Invoices at risk in the lineage baseline that are no longer at risk in `check_id`.

    Baseline is the first completed check that shares ledger keys with the viewed
    check — not merely the chronologically first check of the period. Seed (or any
    prior upload with a different invoice set) must not become the baseline for a
    later GSP/demo run on the same period, or every correction reads as 0.

    Keyed intersection — a 2B-only re-run that writes no ledger rows reports 0, not
    "everything corrected". Once the lineage baseline is set, later re-runs
    accumulate against it.
    """
    completed_ids = (
        await db.execute(
            select(ReconciliationCheck.id)
            .where(
                ReconciliationCheck.period_id == period_id,
                ReconciliationCheck.status == CheckStatus.COMPLETED.value,
            )
            .order_by(ReconciliationCheck.created_at.asc())
        )
    ).scalars().all()
    if not completed_ids:
        return 0, Decimal(0)

    ledger_rows = (
        await db.execute(
            select(Invoice).where(
                Invoice.period_id == period_id,
                Invoice.check_id.in_(completed_ids),
                # Matched pair is two rows; same double-count guard as period_headline.
                Invoice.source != InvoiceSource.GSTR2B.value,
            )
        )
    ).scalars().all()

    by_check: dict = {}
    for inv in ledger_rows:
        by_check.setdefault(inv.check_id, {})[_invoice_key(inv.vendor_gstin, inv.invoice_number)] = inv

    viewed_by_key = by_check.get(check_id) or {}
    if not viewed_by_key:
        return 0, Decimal(0)

    viewed_keys = set(viewed_by_key)
    baseline_id = next(
        (cid for cid in completed_ids if set(by_check.get(cid, ())) & viewed_keys),
        None,
    )
    if baseline_id is None or baseline_id == check_id:
        return 0, Decimal(0)

    baseline_at_risk = {
        key
        for key, inv in by_check[baseline_id].items()
        if inv.status in _AT_RISK_SET
    }
    corrected = [
        viewed_by_key[key]
        for key in baseline_at_risk
        if key in viewed_by_key and viewed_by_key[key].status not in _AT_RISK_SET
    ]
    saved = sum((inv.total_tax for inv in corrected), Decimal(0))
    return len(corrected), Decimal(saved)


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
    invoices_corrected: int
    tax_credit_saved: Decimal


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
    db: AsyncSession,
    period: ReconciliationPeriod,
    check_id=None,
    vendors: list[VendorRiskRow] | None = None,
) -> PeriodHeadline:
    check_id = check_id or await latest_check_id(db, period.id)
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
    invoices_corrected, tax_credit_saved = await corrections_for_check(
        db, period.id, check_id
    )

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
        invoices_corrected=invoices_corrected,
        tax_credit_saved=tax_credit_saved,
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
