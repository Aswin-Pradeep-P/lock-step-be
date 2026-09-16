"""Deterministic tiered matching. No LLM anywhere in this path.

A chartered accountant has to be able to audit every outcome, so each result carries
a `match_reason` that is a human sentence naming the rule that fired — not a score.
Fuzzy similarity is deliberately absent: "87% confident" is not auditable.

Tiers, in order:
  1. EXACT_MATCH        GSTIN + invoice number + date (±3d) + total tax (±₹1)
  2. CLERICAL_MISMATCH  GSTIN + amount agree, invoice number differs only clerically
  3. AMOUNT_MISMATCH    GSTIN + invoice number agree, tax differs
  4. MISSING_IN_GSTR2B  in the ledger, absent from 2B — the supplier has not filed
  5. MISSING_IN_LEDGER  in 2B, absent from books
  6. ITC_INELIGIBLE     in 2B but ITC Availability says no
  7. DUPLICATE          same invoice number twice for one vendor in a period
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from lockstep.models.enums import InvoiceMatchStatus
from lockstep.services.ingestion import CanonicalRow
from lockstep.services.risk_rules import sec17_5_hint

#: ±₹1 absolute, for rounding only. Not a percentage — a 1% tolerance on ₹10,00,000
#: silently swallows a ₹10,000 error.
AMOUNT_TOLERANCE = Decimal("1.00")
DATE_TOLERANCE_DAYS = 3


@dataclass
class MatchResult:
    ledger_row: CanonicalRow | None
    gstr2b_row: CanonicalRow | None
    status: InvoiceMatchStatus
    match_reason: str


def _amounts_close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= AMOUNT_TOLERANCE


def _dates_close(a: CanonicalRow, b: CanonicalRow) -> bool:
    if a.invoice_date is None or b.invoice_date is None:
        return True  # a missing date is not evidence of a mismatch
    return abs((a.invoice_date - b.invoice_date).days) <= DATE_TOLERANCE_DAYS


def _money(amount: Decimal) -> str:
    return f"₹{amount:,.2f}"


def _duplicates(rows: list[CanonicalRow]) -> set[int]:
    """Row indexes that repeat (vendor GSTIN, invoice number) — keep the first."""
    seen: dict[tuple[str, str], int] = {}
    duplicates: set[int] = set()
    for i, row in enumerate(rows):
        key = (row.gstin_normalized, row.invoice_number_normalized)
        if key in seen:
            duplicates.add(i)
        else:
            seen[key] = i
    return duplicates


def resolve_missing_gstins(
    ledger_rows: list[CanonicalRow],
    gstr2b_rows: list[CanonicalRow],
    known_vendors: dict[str, str] | None = None,
) -> int:
    """Fill in GSTINs a Tally export never had, from the party name.

    A purchase register typically carries "Buyer/Supplier" — a name — and no GSTIN at
    all. Matching keys on GSTIN, so without this every row would read as
    MISSING_IN_GSTR2B and the whole reconciliation would be noise.

    Mutates `ledger_rows` in place and returns how many were resolved. Names are matched
    on the normalised form, so 'CloudNine Technologies Pvt Ltd' finds 'CLOUDNINE
    TECHNOLOGIES'. An unresolved row keeps its blank GSTIN and is simply not matched by
    GSTIN — never guessed at.
    """
    by_name: dict[str, str] = dict(known_vendors or {})
    for row in gstr2b_rows:
        if row.vendor_name_normalized and row.gstin_normalized:
            by_name.setdefault(row.vendor_name_normalized, row.gstin_normalized)

    resolved = 0
    for row in ledger_rows:
        if row.gstin_normalized or not row.vendor_name_normalized:
            continue
        gstin = by_name.get(row.vendor_name_normalized)
        if gstin:
            row.gstin = gstin
            row.gstin_normalized = gstin
            resolved += 1
    return resolved


def match_invoices(
    ledger_rows: list[CanonicalRow], gstr2b_rows: list[CanonicalRow]
) -> list[MatchResult]:
    results: list[MatchResult] = []
    ledger_dupes = _duplicates(ledger_rows)
    unmatched_2b = {
        i for i in range(len(gstr2b_rows))
    }
    unmatched_ledger: list[int] = []

    # Tier 1 — exact on (GSTIN, invoice number), amount within ±₹1, date within ±3 days.
    exact_index: dict[tuple[str, str], list[int]] = {}
    for i, row in enumerate(gstr2b_rows):
        exact_index.setdefault((row.gstin_normalized, row.invoice_number_normalized), []).append(i)

    for l_idx, ledger_row in enumerate(ledger_rows):
        if l_idx in ledger_dupes:
            results.append(
                MatchResult(
                    ledger_row, None, InvoiceMatchStatus.DUPLICATE,
                    f"Invoice {ledger_row.invoice_number} appears more than once for this "
                    f"vendor in the purchase ledger for this period.",
                )
            )
            continue

        key = (ledger_row.gstin_normalized, ledger_row.invoice_number_normalized)
        candidates = [i for i in exact_index.get(key, []) if i in unmatched_2b]
        matched = next(
            (
                i for i in candidates
                if _amounts_close(ledger_row.total_tax, gstr2b_rows[i].total_tax)
                and _dates_close(ledger_row, gstr2b_rows[i])
            ),
            None,
        )
        if matched is not None:
            unmatched_2b.discard(matched)
            results.append(
                _classify_matched_pair(
                    ledger_row, gstr2b_rows[matched], InvoiceMatchStatus.EXACT_MATCH,
                    f"Invoice {ledger_row.invoice_number} matches GSTR-2B on vendor GSTIN, "
                    f"invoice number, date and {_money(gstr2b_rows[matched].total_tax)} of tax.",
                )
            )
            continue

        # Tier 3 — same invoice number and GSTIN, tax differs. A wrong amount, not a typo.
        if candidates:
            g_idx = candidates[0]
            unmatched_2b.discard(g_idx)
            gstr2b_row = gstr2b_rows[g_idx]
            delta = gstr2b_row.total_tax - ledger_row.total_tax
            results.append(
                MatchResult(
                    ledger_row, gstr2b_row, InvoiceMatchStatus.AMOUNT_MISMATCH,
                    f"Invoice number and GSTIN match, but tax differs by {_money(abs(delta))} "
                    f"({_money(ledger_row.total_tax)} in your books vs "
                    f"{_money(gstr2b_row.total_tax)} in GSTR-2B).",
                )
            )
            continue

        unmatched_ledger.append(l_idx)

    # Tier 2 — clerical: GSTIN and amount agree, invoice number differs only by case,
    # spacing, leading zeros or O/0-style confusions.
    clerical_index: dict[tuple[str, str], list[int]] = {}
    for i in unmatched_2b:
        row = gstr2b_rows[i]
        clerical_index.setdefault((row.gstin_normalized, row.clerical_key), []).append(i)

    still_unmatched_ledger: list[int] = []
    for l_idx in unmatched_ledger:
        ledger_row = ledger_rows[l_idx]
        key = (ledger_row.gstin_normalized, ledger_row.clerical_key)
        candidates = [i for i in clerical_index.get(key, []) if i in unmatched_2b]
        matched = next(
            (
                i for i in candidates
                if _amounts_close(ledger_row.total_tax, gstr2b_rows[i].total_tax)
                and _dates_close(ledger_row, gstr2b_rows[i])
            ),
            None,
        )
        if matched is None:
            still_unmatched_ledger.append(l_idx)
            continue
        unmatched_2b.discard(matched)
        gstr2b_row = gstr2b_rows[matched]
        results.append(
            _classify_matched_pair(
                ledger_row, gstr2b_row, InvoiceMatchStatus.CLERICAL_MISMATCH,
                f"Same vendor and same tax, but the invoice number is written "
                f"'{ledger_row.invoice_number}' in your books and "
                f"'{gstr2b_row.invoice_number}' in GSTR-2B.",
            )
        )

    # Tier 4 — in the ledger, absent from 2B. The supplier has not filed.
    # This is the one that matters: actionable before the 13th.
    for l_idx in still_unmatched_ledger:
        ledger_row = ledger_rows[l_idx]
        reason = (
            f"Invoice {ledger_row.invoice_number} is in your purchase ledger but has not "
            f"appeared in GSTR-2B — {ledger_row.vendor_name or 'the supplier'} has not "
            f"filed it. {_money(ledger_row.itc_amount)} of ITC is at risk."
        )
        # There's no 2B row here to check ITC Availability against, but the
        # description can still carry a hint that this was never claimable to begin
        # with — advisory only, it never changes the status: a CA must still verify.
        if sec17_5_hint(ledger_row.description):
            reason += (
                " This description also matches a category commonly blocked under "
                "Sec 17(5) — verify eligibility independently of whether the vendor files."
            )
        results.append(
            MatchResult(ledger_row, None, InvoiceMatchStatus.MISSING_IN_GSTR2B, reason)
        )

    # Tiers 5 and 6 — 2B rows nobody claimed.
    for g_idx in sorted(unmatched_2b):
        gstr2b_row = gstr2b_rows[g_idx]
        if gstr2b_row.itc_available is False:
            reason = gstr2b_row.itc_reason or "the portal marks it unavailable"
            results.append(
                MatchResult(
                    None, gstr2b_row, InvoiceMatchStatus.ITC_INELIGIBLE,
                    f"Invoice {gstr2b_row.invoice_number} is in GSTR-2B but ITC is not "
                    f"available: {reason}.",
                )
            )
            continue
        results.append(
            MatchResult(
                None, gstr2b_row, InvoiceMatchStatus.MISSING_IN_LEDGER,
                f"Invoice {gstr2b_row.invoice_number} from "
                f"{gstr2b_row.vendor_name or gstr2b_row.gstin} is in GSTR-2B but not in your "
                f"books — either an unrecorded purchase or someone else's invoice filed "
                f"against your GSTIN.",
            )
        )

    return results


def _classify_matched_pair(
    ledger_row: CanonicalRow,
    gstr2b_row: CanonicalRow,
    ok_status: InvoiceMatchStatus,
    ok_reason: str,
) -> MatchResult:
    """Any ledger/2B pair, with ITC eligibility always taking precedence.

    Used by both Tier 1 (exact) and Tier 2 (clerical) — a matched-but-unclaimable
    invoice is never a clean match or a mere typo, regardless of which tier found it.
    Checking this in one shared place means a future tier can't reintroduce the bug
    where a clerical typo silently masked a Sec 17(5)/ITC-blocked invoice.
    """
    if gstr2b_row.itc_available is False:
        reason = gstr2b_row.itc_reason or "the portal marks it unavailable"
        return MatchResult(
            ledger_row, gstr2b_row, InvoiceMatchStatus.ITC_INELIGIBLE,
            f"Matched to GSTR-2B, but ITC is not available on this invoice: {reason}.",
        )
    return MatchResult(ledger_row, gstr2b_row, ok_status, ok_reason)


def carry_forward_reason(tax_period: str, invoice_number: str) -> str:
    month, year = tax_period[:2], tax_period[2:]
    return (
        f"Invoice {invoice_number} was missing from GSTR-2B in {month}/{year} and has now "
        f"appeared — the supplier filed late, and this credit is claimable this period."
    )
