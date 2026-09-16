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


@dataclass
class VendorMap:
    """Looks a row's vendor up by GSTIN when there is one, else by normalized name.

    Keeping these as two separate maps (rather than one dict keyed on a synthetic
    string) avoids ever having a made-up name collide with a real GSTIN.
    """

    by_gstin: dict[str, Vendor]
    by_name: dict[str, Vendor]

    def get(self, row: CanonicalRow) -> Vendor | None:
        if row.gstin_normalized:
            return self.by_gstin.get(row.gstin_normalized)
        if row.vendor_name_normalized:
            return self.by_name.get(row.vendor_name_normalized)
        return None


def _backfill_contact(vendor: Vendor, email: str, phone: str) -> None:
    if email and not vendor.contact_email:
        vendor.contact_email = email
    if phone and not vendor.contact_phone:
        vendor.contact_phone = phone


async def _upsert_vendors(db: AsyncSession, rows: list[CanonicalRow]) -> VendorMap:
    """One vendor per GSTIN. A row with no resolvable GSTIN — a never-filed supplier
    a Tally export never assigned one to — still gets a durable vendor, keyed on
    normalized name and marked `gstin_verified=False`, instead of being dropped:
    without this there is nothing to hang a risk score or a reminder on for exactly
    the vendors most worth chasing. Contact info and name are backfilled from
    whichever side first supplies them.
    """
    verified: dict[str, dict] = {}
    unverified: dict[str, dict] = {}
    for row in rows:
        bucket, key = (verified, row.gstin_normalized) if row.gstin_normalized else (
            unverified, row.vendor_name_normalized
        )
        if not key:
            continue
        entry = bucket.setdefault(key, {"name": "", "email": "", "phone": ""})
        if row.vendor_name and not entry["name"]:
            entry["name"] = row.vendor_name
        if row.vendor_email and not entry["email"]:
            entry["email"] = row.vendor_email
        if row.vendor_phone and not entry["phone"]:
            entry["phone"] = row.vendor_phone

    if not verified and not unverified:
        return VendorMap({}, {})

    existing_by_gstin: dict[str, Vendor] = {}
    if verified:
        rows_ = (
            await db.execute(select(Vendor).where(Vendor.gstin.in_(verified)))
        ).scalars().all()
        existing_by_gstin = {v.gstin: v for v in rows_}

    # Names to check for a possible unverified -> verified upgrade: every verified
    # row's own name (its vendor may already exist, recorded unverified from a
    # period before this GSTIN was ever known), plus every genuinely-unverified
    # row's name.
    candidate_names = set(unverified) | {
        normalize_name(info["name"]) for info in verified.values() if info["name"]
    }
    existing_by_name: dict[str, Vendor] = {}
    if candidate_names:
        rows_ = (
            await db.execute(select(Vendor).where(Vendor.unverified_key.in_(candidate_names)))
        ).scalars().all()
        existing_by_name = {v.unverified_key: v for v in rows_}

    for gstin, info in verified.items():
        vendor = existing_by_gstin.get(gstin)
        if vendor is None:
            # This exact vendor may already be sitting in the table unverified —
            # upgrade that row in place (keeping its id, and anything already
            # linked to it) instead of leaving it stale and creating a duplicate.
            name_key = normalize_name(info["name"]) if info["name"] else None
            upgrade = existing_by_name.get(name_key) if name_key else None
            if upgrade is not None and upgrade.gstin is None:
                vendor = upgrade
                vendor.gstin = gstin
                vendor.gstin_verified = True
                vendor.unverified_key = None
                if info["name"] and vendor.name == "Unknown vendor":
                    vendor.name = info["name"]
            else:
                vendor = Vendor(gstin=gstin, gstin_verified=True, name=info["name"] or gstin)
                db.add(vendor)
            existing_by_gstin[gstin] = vendor
            if name_key:
                existing_by_name[name_key] = vendor  # same row, still reachable by name
        elif info["name"] and vendor.name == vendor.gstin:
            vendor.name = info["name"]  # we learned a real name for a GSTIN-only vendor
        _backfill_contact(vendor, info["email"], info["phone"])

    for key, info in unverified.items():
        vendor = existing_by_name.get(key)
        if vendor is None:
            vendor = Vendor(
                gstin=None, gstin_verified=False, unverified_key=key,
                name=info["name"] or "Unknown vendor",
            )
            db.add(vendor)
            existing_by_name[key] = vendor
        elif info["name"] and vendor.name == "Unknown vendor":
            vendor.name = info["name"]
        _backfill_contact(vendor, info["email"], info["phone"])

    await db.flush()
    return VendorMap(existing_by_gstin, existing_by_name)


def _invoice_from(
    row: CanonicalRow,
    *,
    check_id: uuid.UUID,
    period_id: uuid.UUID,
    source: InvoiceSource,
    status: InvoiceMatchStatus,
    match_reason: str,
    vendor: Vendor | None,
    carried_from_period: str | None = None,
    is_reverse_charge: bool | None = None,
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
        is_reverse_charge=row.is_reverse_charge if is_reverse_charge is None else is_reverse_charge,
        supplier_filed_at=row.supplier_filed_at,
        status=status,
        match_reason=match_reason,
        carried_from_period=carried_from_period,
        description=row.description or None,
        recoverable_until=(
            compute_recoverable_until(row.invoice_date) if row.invoice_date else None
        ),
        raw_data=row.raw,
    )


async def _resolve_carry_forward(
    db: AsyncSession,
    check: ReconciliationCheck,
    period: ReconciliationPeriod,
    gstr2b_rows: list[CanonicalRow],
    vendors: VendorMap,
) -> int:
    """Last period's unresolved MISSING_IN_GSTR2B rows that now appear in this 2B.

    Showing a user that last month's stuck credit finally landed is a genuinely good
    moment, so it gets its own status rather than quietly disappearing — and it has to
    land as a row in *this* check/period, since that is the only place a CA looking at
    the current dashboard will ever see it. The old period's row is marked RESOLVED:
    its own problem (the vendor hadn't filed) is settled, but it was never itself
    "carried from" anywhere, so it does not get that tag.
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

    from lockstep.services.ingestion import normalize_invoice_number

    appeared = {(r.gstin_normalized, r.invoice_number_normalized): r for r in gstr2b_rows}
    carried = 0
    for invoice in stuck:
        key = (invoice.vendor_gstin or "", normalize_invoice_number(invoice.invoice_number))
        row = appeared.get(key)
        if row is None:
            continue
        invoice.status = InvoiceMatchStatus.RESOLVED
        invoice.match_reason = (
            f"{invoice.match_reason} Filed late — now claimed in "
            f"{period.tax_period[:2]}/{period.tax_period[2:]}."
        )
        db.add(
            _invoice_from(
                row, check_id=check.id, period_id=period.id,
                source=InvoiceSource.GSTR2B, status=InvoiceMatchStatus.CARRIED_FORWARD,
                match_reason=carry_forward_reason(previous, invoice.invoice_number),
                vendor=vendors.get(row),
                carried_from_period=previous,
            )
        )
        carried += 1
    return carried


async def run_check(
    db: AsyncSession,
    check: ReconciliationCheck,
    period: ReconciliationPeriod,
    ledger_file: tuple[str, bytes] | None,
    gstr2b_file: tuple[str, bytes] | None,
) -> CheckOutcome:
    """Parse both sides from uploaded files, then match and persist. Caller commits."""
    ledger_rows: list[CanonicalRow] = []
    gstr2b_rows: list[CanonicalRow] = []
    column_mapping: dict = {}

    if ledger_file:
        rows, mapping = parse_file(*ledger_file, hint="ledger")
        ledger_rows = to_canonical_rows(rows, mapping)
        column_mapping["ledger"] = mapping
    if gstr2b_file:
        rows, mapping = parse_file(*gstr2b_file, hint="gstr2b")
        gstr2b_rows = to_canonical_rows(rows, mapping)
        column_mapping["gstr2b"] = mapping

    return await run_check_from_rows(db, check, period, ledger_rows, gstr2b_rows, column_mapping)


async def run_check_from_rows(
    db: AsyncSession,
    check: ReconciliationCheck,
    period: ReconciliationPeriod,
    ledger_rows: list[CanonicalRow],
    gstr2b_rows: list[CanonicalRow],
    column_mapping: dict | None = None,
) -> CheckOutcome:
    """Match and persist one check from already-parsed rows, whichever source they
    came from — an uploaded file (`run_check`) or a live GSP API fetch
    (`api/v1/periods.py`'s GSP-fetch endpoint). Everything from here down is
    identical either way: matching stays 100% rule-based regardless of source.
    Caller commits.
    """
    column_mapping = column_mapping or {}

    # A Tally ledger often has no GSTIN column. Resolve by party name against this
    # 2B and against vendors we already know, before anything keys on GSTIN. Only
    # verified vendors can ever be a source of a GSTIN here — an unverified one (no
    # GSTIN was ever resolvable for them yet) would otherwise poison this dict with
    # None for that name and permanently block it from ever being resolved later,
    # even once a check finally supplies the real GSTIN for that exact vendor name.
    known = {
        normalize_name(v.name): v.gstin
        for v in (await db.execute(select(Vendor).where(Vendor.gstin_verified))).scalars().all()
    }
    resolve_missing_gstins(ledger_rows, gstr2b_rows, known)

    vendors = await _upsert_vendors(db, ledger_rows + gstr2b_rows)
    carried = await _resolve_carry_forward(db, check, period, gstr2b_rows, vendors)

    written = 0
    for result in match_invoices(ledger_rows, gstr2b_rows):
        ledger_row, gstr2b_row = result.ledger_row, result.gstr2b_row

        if ledger_row is not None and gstr2b_row is not None:
            # A matched pair is two rows pointing at each other, so the UI can render
            # ledger vs 2B side by side. Reverse charge is combined across both sides
            # before either row is built: a Tally ledger never carries this column,
            # so only ever trusting the ledger side (as the list endpoint's default
            # display row does) silently lost a true "Y" from GSTR-2B.
            combined_rc = ledger_row.is_reverse_charge or gstr2b_row.is_reverse_charge
            left = _invoice_from(
                ledger_row, check_id=check.id, period_id=period.id,
                source=InvoiceSource.LEDGER, status=result.status,
                match_reason=result.match_reason,
                vendor=vendors.get(ledger_row),
                is_reverse_charge=combined_rc,
            )
            right = _invoice_from(
                gstr2b_row, check_id=check.id, period_id=period.id,
                source=InvoiceSource.GSTR2B, status=result.status,
                match_reason=result.match_reason,
                vendor=vendors.get(gstr2b_row),
                is_reverse_charge=combined_rc,
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
                vendor=vendors.get(row),
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
