"""Seed a realistic demo dataset: 20 vendors, 6 periods of filing history, ~300 invoices
across every status.

This data is SEEDED, not real. Say so in the demo — it is cheaper than being asked.

    uv run python -m lockstep.seed [--reset]
"""

from __future__ import annotations

import argparse
import asyncio
import random
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import delete, select

from lockstep.core.security import hash_password
from lockstep.database import AsyncSessionLocal
from lockstep.models.enums import (
    ActionType,
    CheckStatus,
    InvoiceMatchStatus,
    InvoiceSource,
)
from lockstep.models.invoice import Invoice
from lockstep.models.invoice_action import InvoiceAction
from lockstep.models.period import Client, ReconciliationCheck, ReconciliationPeriod
from lockstep.models.user import User
from lockstep.models.vendor import Vendor
from lockstep.models.vendor_filing_history import VendorFilingHistory
from lockstep.services.matching import carry_forward_reason
from lockstep.services.periods import (
    days_past_cutoff,
    format_tax_period,
    period_dates,
    previous_tax_period,
    today_ist,
)
from lockstep.services.risk_rules import compute_recoverable_until

SEED = 20260916  # deterministic: the demo looks the same every run
DEMO_EMAIL = "demo@lockstep.test"
DEMO_PASSWORD = "lockstep"

VENDOR_NAMES = [
    "Sharma Traders", "Patel Industries", "Kumar Enterprises", "Reddy Textiles",
    "Mehta Steel Works", "Iyer Logistics", "Singh Packaging", "Desai Chemicals",
    "Nair Electricals", "Gupta Paper Mills", "Bose Instruments", "Chopra Auto Parts",
    "Rao Agro Foods", "Joshi Plastics", "Verma Hardware", "Pillai Marine Exports",
    "Banerjee Print House", "Malhotra Furnishings", "Shetty Cold Storage",
    "Thakur Construction",
]

STATE_CODES = ["29", "27", "33", "07", "24", "19", "36", "06"]
#: on_time_rate the seeded history should land near, per vendor archetype.
RELIABILITY = (
    [0.95] * 8      # dependable filers -> LOW
    + [0.75] * 6    # drifters          -> MEDIUM
    + [0.35] * 6    # chronic late      -> HIGH
)
ITC_REASONS = [
    "POS and supplier state are same but recipient state is different",
    "Return filed after the expiry of the time limit",
    "Supplier has not paid tax on this invoice",
]


def _gstin(rng: random.Random, index: int) -> str:
    state = STATE_CODES[index % len(STATE_CODES)]
    letters = "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(5))
    digits = "".join(rng.choice("0123456789") for _ in range(4))
    return f"{state}{letters}{digits}{rng.choice('ABCDEFGHJ')}1Z{rng.randint(1, 9)}"


def _recent_periods(count: int) -> list[str]:
    """`count` tax periods ending with the one currently open."""
    today = today_ist()
    periods = []
    month, year = today.month, today.year
    for _ in range(count):
        periods.append(format_tax_period(month, year))
        month, year = (12, year - 1) if month == 1 else (month - 1, year)
    return list(reversed(periods))


def _money(rng: random.Random, low: int, high: int) -> Decimal:
    return Decimal(rng.randrange(low, high, 100)).quantize(Decimal("0.01"))


async def seed(reset: bool = False) -> None:
    rng = random.Random(SEED)

    async with AsyncSessionLocal() as db:
        if reset:
            for model in (
                InvoiceAction, Invoice, ReconciliationCheck, ReconciliationPeriod,
                VendorFilingHistory, Vendor, Client,
            ):
                await db.execute(delete(model))
            await db.commit()
        elif (await db.execute(select(Vendor.id).limit(1))).scalar_one_or_none():
            # Idempotent: re-running the start script must not duplicate the demo data.
            print("Already seeded — run with --reset to rebuild.")
            return

        user = (
            await db.execute(select(User).where(User.email == DEMO_EMAIL))
        ).scalar_one_or_none()
        if user is None:
            user = User(
                email=DEMO_EMAIL,
                hashed_password=hash_password(DEMO_PASSWORD),
                full_name="Demo CA",
            )
            db.add(user)
            await db.flush()

        org_id = uuid.UUID(int=0)
        client = (
            await db.execute(select(Client).where(Client.org_id == org_id))
        ).scalar_one_or_none()
        if client is None:
            client = Client(
                org_id=org_id, legal_name="Acme Manufacturing Pvt Ltd", gstin="29AACCA1234M1Z7"
            )
            db.add(client)
            await db.flush()

        vendors: list[Vendor] = []
        for i, name in enumerate(VENDOR_NAMES):
            slug = name.split()[0].lower()
            vendor = Vendor(
                gstin=_gstin(rng, i),
                name=name,
                contact_email=f"accounts@{slug}.example",
                contact_phone=f"+91 9{rng.randrange(100000000, 999999999)}",
            )
            db.add(vendor)
            vendors.append(vendor)
        await db.flush()

        periods = _recent_periods(6)
        current_period = periods[-1]

        # --- 6 periods of filing history: the table the differentiation rests on -------
        for vendor, reliability in zip(vendors, RELIABILITY, strict=True):
            for tax_period in periods:
                cutoff = period_dates(tax_period)["cutoff_date"]
                on_time = rng.random() < reliability
                if tax_period == current_period and not on_time:
                    # The open period: a late filer simply has not filed yet.
                    filed_at = None
                else:
                    offset = -rng.randint(1, 6) if on_time else rng.randint(2, 9)
                    filed_at = cutoff + timedelta(days=offset)
                db.add(
                    VendorFilingHistory(
                        vendor_id=vendor.id,
                        tax_period=tax_period,
                        gstr1_filed=filed_at is not None,
                        gstr1_filed_at=filed_at,
                        days_past_cutoff=days_past_cutoff(tax_period, filed_at),
                        invoice_count=rng.randint(2, 9),
                    )
                )

        # --- Periods, checks and invoices ---------------------------------------------
        invoice_seq = 1000
        for tax_period in periods[-3:]:  # three periods of invoice detail is plenty
            dates = period_dates(tax_period)
            period = ReconciliationPeriod(
                client_id=client.id,
                tax_period=tax_period,
                cutoff_date=dates["cutoff_date"],
                gstr2b_date=dates["gstr2b_date"],
                filing_due=dates["filing_due"],
            )
            db.add(period)
            await db.flush()

            is_current = tax_period == current_period

            # Build the period's invoice set once, then materialise it per check.
            ledger: list[tuple[Vendor, str, Decimal, Decimal, date, InvoiceMatchStatus]] = []
            for vendor, reliability in zip(vendors, RELIABILITY, strict=True):
                for _ in range(rng.randint(3, 8)):
                    invoice_seq += 1
                    taxable = _money(rng, 20_000, 900_000)
                    ledger.append((
                        vendor,
                        f"INV-{invoice_seq}",
                        taxable,
                        (taxable * Decimal("0.18")).quantize(Decimal("0.01")),
                        dates["cutoff_date"] - timedelta(days=rng.randint(15, 40)),
                        _pick_status(rng, reliability, is_current),
                    ))

            # The current period has been re-checked as vendors filed. Each successive
            # check finds fewer invoices missing — that closing gap is the delta view,
            # and it is the thing a one-shot run cannot express.
            checks = [(10, 0.0), (3, 0.35), (0, 0.6)] if is_current else [(0, 0.0)]
            for days_ago, filed_since in checks:
                check = ReconciliationCheck(
                    period_id=period.id,
                    created_by=user.id,
                    status=CheckStatus.COMPLETED.value,
                    rows_parsed=len(ledger),
                    column_mapping={"ledger": {"invoice_number": "Voucher No."}},
                )
                db.add(check)
                await db.flush()
                # created_at has a server default; override so the checks are ordered.
                check.created_at = check.created_at - timedelta(days=days_ago)

                for vendor, number, taxable, tax, invoice_date, base_status in ledger:
                    status = base_status
                    if (
                        base_status == InvoiceMatchStatus.MISSING_IN_GSTR2B
                        and rng.random() < filed_since
                    ):
                        status = InvoiceMatchStatus.EXACT_MATCH  # the vendor has since filed
                    db.add_all(
                        _build_rows(
                            rng, status, check.id, period.id, vendor, number,
                            invoice_date, taxable, tax, tax_period,
                        )
                    )

        await db.flush()

        # --- A few actions, so the audit trail is not empty on the demo ---------------
        at_risk = (
            await db.execute(
                select(Invoice)
                .where(Invoice.status == InvoiceMatchStatus.MISSING_IN_GSTR2B)
                .limit(12)
            )
        ).scalars().all()
        for invoice in at_risk[:8]:
            db.add(
                InvoiceAction(
                    invoice_id=invoice.id,
                    action=ActionType.VENDOR_NOTIFIED,
                    channel="email",
                    auto_proposed=True,
                    amount_at_risk=invoice.total_tax,
                    payload={"note": "Seeded demo action"},
                )
            )
        for invoice in at_risk[8:11]:
            db.add(
                InvoiceAction(
                    invoice_id=invoice.id,
                    action=ActionType.PAYMENT_HOLD_PROPOSED,
                    auto_proposed=True,
                    amount_at_risk=invoice.total_tax,
                )
            )

        await db.commit()

        counts = {
            "vendors": len(vendors),
            "periods": len(periods),
            "invoices": (
                await db.execute(select(Invoice.id))
            ).scalars().all().__len__(),
        }
        print(
            f"Seeded (deterministic, SEED={SEED}): {counts['vendors']} vendors, "
            f"6 periods of filing history, {counts['invoices']} invoices.\n"
            f"Login: {DEMO_EMAIL} / {DEMO_PASSWORD}\n"
            f"Current tax period: {current_period}. This data is seeded, not real."
        )


def _pick_status(
    rng: random.Random, reliability: float, is_current: bool
) -> InvoiceMatchStatus:
    """A deliberate mix of every status, weighted by how reliable the vendor is."""
    roll = rng.random()
    missing_chance = 0.45 if reliability < 0.5 else (0.2 if reliability < 0.9 else 0.06)
    if roll < missing_chance:
        return InvoiceMatchStatus.MISSING_IN_GSTR2B
    if roll < missing_chance + 0.08:
        return InvoiceMatchStatus.CLERICAL_MISMATCH
    if roll < missing_chance + 0.13:
        return InvoiceMatchStatus.AMOUNT_MISMATCH
    if roll < missing_chance + 0.16:
        return InvoiceMatchStatus.MISSING_IN_LEDGER
    if roll < missing_chance + 0.19:
        return InvoiceMatchStatus.ITC_INELIGIBLE
    if roll < missing_chance + 0.21:
        return InvoiceMatchStatus.DUPLICATE
    if roll < missing_chance + 0.24 and is_current:
        return InvoiceMatchStatus.CARRIED_FORWARD
    if roll < missing_chance + 0.26:
        return InvoiceMatchStatus.RESOLVED
    return InvoiceMatchStatus.EXACT_MATCH


def _build_rows(
    rng: random.Random,
    status: InvoiceMatchStatus,
    check_id,
    period_id,
    vendor: Vendor,
    number: str,
    invoice_date: date,
    taxable: Decimal,
    tax: Decimal,
    tax_period: str,
) -> list[Invoice]:
    """Build the invoice row(s) for one seeded outcome. Matched statuses produce a pair."""
    interstate = rng.random() < 0.4

    def row(
        source: InvoiceSource,
        *,
        number_override: str | None = None,
        tax_override: Decimal | None = None,
        reason: str,
        itc_available: bool | None = None,
        itc_reason: str | None = None,
        filed_at: date | None = None,
        carried: str | None = None,
    ) -> Invoice:
        row_tax = tax_override if tax_override is not None else tax
        row_split = (row_tax / 2).quantize(Decimal("0.01"))
        return Invoice(
            id=uuid.uuid4(),
            check_id=check_id,
            period_id=period_id,
            vendor_id=vendor.id,
            vendor_gstin=vendor.gstin,
            source=source,
            invoice_number=number_override or number,
            invoice_date=invoice_date,
            taxable_value=taxable,
            igst=row_tax if interstate else Decimal("0"),
            cgst=Decimal("0") if interstate else row_split,
            sgst=Decimal("0") if interstate else row_split,
            cess=Decimal("0"),
            itc_available=itc_available,
            itc_reason=itc_reason,
            is_reverse_charge=rng.random() < 0.06,
            supplier_filed_at=filed_at,
            status=status,
            match_reason=reason,
            carried_from_period=carried,
            recoverable_until=compute_recoverable_until(invoice_date),
            raw_data={
                "Invoice No": number_override or number,
                "Supplier": vendor.name,
                "GSTIN": vendor.gstin,
                "Taxable Value": str(taxable),
            },
        )

    filed_at = period_dates(tax_period)["cutoff_date"] - timedelta(days=rng.randint(1, 5))

    if status == InvoiceMatchStatus.MISSING_IN_GSTR2B:
        return [row(
            InvoiceSource.LEDGER,
            reason=(
                f"Invoice {number} is in your purchase ledger but has not appeared in "
                f"GSTR-2B — {vendor.name} has not filed it. ₹{tax:,.2f} of ITC is at risk."
            ),
        )]

    if status == InvoiceMatchStatus.MISSING_IN_LEDGER:
        return [row(
            InvoiceSource.GSTR2B, filed_at=filed_at,
            reason=(
                f"Invoice {number} from {vendor.name} is in GSTR-2B but not in your books — "
                f"either an unrecorded purchase or someone else's invoice filed against "
                f"your GSTIN."
            ),
        )]

    if status == InvoiceMatchStatus.ITC_INELIGIBLE:
        reason_text = rng.choice(ITC_REASONS)
        return [row(
            InvoiceSource.GSTR2B, filed_at=filed_at, itc_available=False,
            itc_reason=reason_text,
            reason=f"Invoice {number} is in GSTR-2B but ITC is not available: {reason_text}.",
        )]

    if status == InvoiceMatchStatus.DUPLICATE:
        return [row(
            InvoiceSource.LEDGER,
            reason=(
                f"Invoice {number} appears more than once for this vendor in the purchase "
                f"ledger for this period."
            ),
        )]

    if status == InvoiceMatchStatus.CARRIED_FORWARD:
        previous = previous_tax_period(tax_period)
        return [row(
            InvoiceSource.LEDGER, filed_at=filed_at, carried=previous,
            reason=carry_forward_reason(previous, number),
        )]

    if status == InvoiceMatchStatus.RESOLVED:
        return [row(
            InvoiceSource.LEDGER, filed_at=filed_at,
            reason=f"Invoice {number} was chased and resolved with the supplier.",
        )]

    if status == InvoiceMatchStatus.CLERICAL_MISMATCH:
        typo = number.replace("INV-", "INV-0")
        reason = (
            f"Same vendor and same tax, but the invoice number is written '{number}' in "
            f"your books and '{typo}' in GSTR-2B."
        )
        left = row(InvoiceSource.LEDGER, reason=reason)
        right = row(
            InvoiceSource.GSTR2B, number_override=typo, reason=reason,
            filed_at=filed_at, itc_available=True,
        )
        left.matched_invoice_id, right.matched_invoice_id = right.id, left.id
        return [left, right]

    if status == InvoiceMatchStatus.AMOUNT_MISMATCH:
        reported = (tax * Decimal(str(rng.choice([1.1, 0.9, 1.25])))).quantize(Decimal("0.01"))
        reason = (
            f"Invoice number and GSTIN match, but tax differs by "
            f"₹{abs(reported - tax):,.2f} (₹{tax:,.2f} in your books vs "
            f"₹{reported:,.2f} in GSTR-2B)."
        )
        left = row(InvoiceSource.LEDGER, reason=reason)
        right = row(
            InvoiceSource.GSTR2B, tax_override=reported, reason=reason,
            filed_at=filed_at, itc_available=True,
        )
        left.matched_invoice_id, right.matched_invoice_id = right.id, left.id
        return [left, right]

    reason = (
        f"Invoice {number} matches GSTR-2B on vendor GSTIN, invoice number, date and "
        f"₹{tax:,.2f} of tax."
    )
    left = row(InvoiceSource.LEDGER, reason=reason)
    right = row(InvoiceSource.GSTR2B, reason=reason, filed_at=filed_at, itc_available=True)
    left.matched_invoice_id, right.matched_invoice_id = right.id, left.id
    return [left, right]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset", action="store_true", help="delete existing demo data first"
    )
    args = parser.parse_args()
    asyncio.run(seed(reset=args.reset))


if __name__ == "__main__":
    main()
