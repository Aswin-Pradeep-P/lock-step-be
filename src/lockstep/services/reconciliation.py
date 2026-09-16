"""The reconciliation pipeline for one check.

Order matters: carry-forward is resolved *before* this period's own matching, so an
invoice that was stuck last month and has now appeared reads as good news rather than
as a fresh mismatch.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.models.enums import CheckStatus, InvoiceMatchStatus, InvoiceSource
from lockstep.models.invoice import Invoice
from lockstep.models.period import ReconciliationCheck, ReconciliationPeriod
from lockstep.models.vendor import Vendor
from lockstep.services import filing_history
from lockstep.services.ingestion import (
    CanonicalRow,
    normalize_name,
    parse_file,
    to_canonical_rows,
)
from lockstep.services.matching import (
    carry_forward_reason,
    match_invoices,
    resolve_missing_gstins,
)
from lockstep.services.periods import previous_tax_period
from lockstep.services.risk_rules import compute_recoverable_until


@dataclass
class CheckOutcome:
    rows_parsed: int
    invoices_written: int
    carried_forward: int
    vendors_observed: int
    column_mapping: dict


async def _upsert_vendors(db: AsyncSession, rows: list[CanonicalRow]) -> dict[str, Vendor]:
    """One vendor per GSTIN. Names come from whichever side supplied one."""
    names: dict[str, str] = {}
    for row in rows:
        if not row.gstin_normalized:
            continue
        if row.vendor_name and not names.get(row.gstin_normalized):
            names[row.gstin_normalized] = row.vendor_name
        names.setdefault(row.gstin_normalized, "")

    if not names:
        return {}

    existing = (
        await db.execute(select(Vendor).where(Vendor.gstin.in_(names)))
    ).scalars().all()
    by_gstin = {v.gstin: v for v in existing}

    for gstin, name in names.items():
        vendor = by_gstin.get(gstin)
        if vendor is None:
            vendor = Vendor(gstin=gstin, name=name or gstin)
            db.add(vendor)
            by_gstin[gstin] = vendor
        elif name and vendor.name == vendor.gstin:
            vendor.name = name  # we learned a real name for a GSTIN-only vendor

    await db.flush()
    return by_gstin


def _invoice_from(
    row: CanonicalRow,
    *,
    check_id: uuid.UUID,
    period_id: uuid.UUID,
    source: InvoiceSource,
    status: InvoiceMatchStatus,
    match_reason: str,
    vendor: Vendor | None,
) -> Invoice:
    return Invoice(
        id=uuid.uuid4(),
        check_id=check_id,
        period_id=period_id,
        vendor_id=vendor.id if vendor else None,
        vendor_gstin=row.gstin_normalized or None,
        source=source,
        invoice_number=row.invoice_number,
        invoice_date=row.invoice_date,
        taxable_value=row.taxable_value,
        igst=row.igst,
        cgst=row.cgst,
        sgst=row.sgst,
        cess=row.cess,
        itc_available=row.itc_available,
        itc_reason=row.itc_reason or None,
        is_reverse_charge=row.is_reverse_charge,
        supplier_filed_at=row.supplier_filed_at,
        status=status,
        match_reason=match_reason,
        recoverable_until=(
            compute_recoverable_until(row.invoice_date) if row.invoice_date else None
        ),
        raw_data=row.raw,
    )


async def _resolve_carry_forward(
    db: AsyncSession,
    period: ReconciliationPeriod,
    gstr2b_rows: list[CanonicalRow],
) -> int:
    """Last period's unresolved MISSING_IN_GSTR2B rows that now appear in this 2B.

    Showing a user that last month's stuck credit finally landed is a genuinely good
    moment, so it gets its own status rather than quietly disappearing.
    """
    previous = previous_tax_period(period.tax_period)
    prior_period = (
        await db.execute(
            select(ReconciliationPeriod).where(
                ReconciliationPeriod.client_id == period.client_id,
                ReconciliationPeriod.tax_period == previous,
            )
        )
    ).scalar_one_or_none()
    if prior_period is None:
        return 0

    stuck = (
        await db.execute(
            select(Invoice).where(
                Invoice.period_id == prior_period.id,
                Invoice.status == InvoiceMatchStatus.MISSING_IN_GSTR2B,
            )
        )
    ).scalars().all()
    if not stuck:
        return 0

    appeared = {(r.gstin_normalized, r.invoice_number_normalized): r for r in gstr2b_rows}
    carried = 0
    for invoice in stuck:
        from lockstep.services.ingestion import normalize_invoice_number

        key = (invoice.vendor_gstin or "", normalize_invoice_number(invoice.invoice_number))
        row = appeared.get(key)
        if row is None:
            continue
        invoice.status = InvoiceMatchStatus.CARRIED_FORWARD
        invoice.carried_from_period = previous
        invoice.match_reason = carry_forward_reason(previous, invoice.invoice_number)
        invoice.supplier_filed_at = row.supplier_filed_at
        carried += 1
    return carried


async def run_check(
    db: AsyncSession,
    check: ReconciliationCheck,
    period: ReconciliationPeriod,
    ledger_file: tuple[str, bytes] | None,
    gstr2b_file: tuple[str, bytes] | None,
) -> CheckOutcome:
    """Parse, match and persist one check. Caller commits."""
    ledger_rows: list[CanonicalRow] = []
    gstr2b_rows: list[CanonicalRow] = []
    column_mapping: dict = {}

    if ledger_file:
        rows, mapping = parse_file(*ledger_file)
        ledger_rows = to_canonical_rows(rows, mapping)
        column_mapping["ledger"] = mapping
    if gstr2b_file:
        rows, mapping = parse_file(*gstr2b_file)
        gstr2b_rows = to_canonical_rows(rows, mapping)
        column_mapping["gstr2b"] = mapping

    # A Tally ledger often has no GSTIN column. Resolve by party name against this
    # 2B and against vendors we already know, before anything keys on GSTIN.
    known = {
        normalize_name(v.name): v.gstin
        for v in (await db.execute(select(Vendor))).scalars().all()
    }
    resolve_missing_gstins(ledger_rows, gstr2b_rows, known)

    vendors = await _upsert_vendors(db, ledger_rows + gstr2b_rows)
    carried = await _resolve_carry_forward(db, period, gstr2b_rows)

    written = 0
    for result in match_invoices(ledger_rows, gstr2b_rows):
        ledger_row, gstr2b_row = result.ledger_row, result.gstr2b_row

        if ledger_row is not None and gstr2b_row is not None:
            # A matched pair is two rows pointing at each other, so the UI can render
            # ledger vs 2B side by side.
            left = _invoice_from(
                ledger_row, check_id=check.id, period_id=period.id,
                source=InvoiceSource.LEDGER, status=result.status,
                match_reason=result.match_reason,
                vendor=vendors.get(ledger_row.gstin_normalized),
            )
            right = _invoice_from(
                gstr2b_row, check_id=check.id, period_id=period.id,
                source=InvoiceSource.GSTR2B, status=result.status,
                match_reason=result.match_reason,
                vendor=vendors.get(gstr2b_row.gstin_normalized),
            )
            left.matched_invoice_id, right.matched_invoice_id = right.id, left.id
            db.add_all([left, right])
            written += 2
            continue

        row = ledger_row or gstr2b_row
        if row is None:
            continue
        db.add(
            _invoice_from(
                row, check_id=check.id, period_id=period.id,
                source=InvoiceSource.LEDGER if ledger_row else InvoiceSource.GSTR2B,
                status=result.status, match_reason=result.match_reason,
                vendor=vendors.get(row.gstin_normalized),
            )
        )
        written += 1

    observed = await filing_history.record_observations(
        db,
        period.tax_period,
        gstr2b_rows,
        ledger_gstins={r.gstin_normalized for r in ledger_rows},
    )

    check.rows_parsed = len(ledger_rows) + len(gstr2b_rows)
    check.column_mapping = column_mapping
    check.status = CheckStatus.COMPLETED.value

    return CheckOutcome(
        rows_parsed=check.rows_parsed,
        invoices_written=written,
        carried_forward=carried,
        vendors_observed=observed,
        column_mapping=column_mapping,
    )
