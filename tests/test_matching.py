"""One named fixture per matching rule.

This is the part a CA will not trust without evidence, and these tests are the evidence.
"""

from datetime import date
from decimal import Decimal

from lockstep.models.enums import InvoiceMatchStatus
from lockstep.services.ingestion import (
    CanonicalRow,
    clerical_key,
    normalize_gstin,
    normalize_invoice_number,
    normalize_name,
)
from lockstep.services.matching import match_invoices

GSTIN_A = "29ABCDE1234F1Z5"
GSTIN_B = "27AAAAA0000A1Z1"


def _row(
    invoice_number: str,
    gstin: str = GSTIN_A,
    tax: str = "1000.00",
    taxable: str = "10000.00",
    invoice_date: date | None = date(2026, 8, 5),
    vendor_name: str = "Sharma Traders",
    itc_available: bool | None = None,
    itc_reason: str = "",
) -> CanonicalRow:
    return CanonicalRow(
        row_index=0,
        invoice_number=invoice_number,
        invoice_number_normalized=normalize_invoice_number(invoice_number),
        clerical_key=clerical_key(invoice_number),
        gstin=gstin,
        gstin_normalized=normalize_gstin(gstin),
        vendor_name=vendor_name,
        vendor_name_normalized=normalize_name(vendor_name),
        taxable_value=Decimal(taxable),
        igst=Decimal(tax),
        cgst=Decimal("0"),
        sgst=Decimal("0"),
        cess=Decimal("0"),
        invoice_date=invoice_date,
        itc_available=itc_available,
        itc_reason=itc_reason,
        raw={"Invoice No": invoice_number},
    )


def _only(results):
    assert len(results) == 1, [r.status for r in results]
    return results[0]


# --- Tier 1: EXACT_MATCH ----------------------------------------------------------

def test_exact_match_on_all_four_fields():
    result = _only(match_invoices([_row("INV-1")], [_row("INV-1")]))
    assert result.status == InvoiceMatchStatus.EXACT_MATCH
    assert "matches GSTR-2B" in result.match_reason


def test_exact_match_tolerates_one_rupee_of_rounding():
    ledger = [_row("INV-1", tax="1000.40")]
    gstr2b = [_row("INV-1", tax="1000.00")]
    assert _only(match_invoices(ledger, gstr2b)).status == InvoiceMatchStatus.EXACT_MATCH


def test_exact_match_tolerates_three_days_of_date_drift():
    ledger = [_row("INV-1", invoice_date=date(2026, 8, 5))]
    gstr2b = [_row("INV-1", invoice_date=date(2026, 8, 8))]
    assert _only(match_invoices(ledger, gstr2b)).status == InvoiceMatchStatus.EXACT_MATCH


# --- Tier 2: CLERICAL_MISMATCH ----------------------------------------------------

def test_clerical_mismatch_on_leading_zeros():
    result = _only(match_invoices([_row("INV-007")], [_row("INV-7")]))
    assert result.status == InvoiceMatchStatus.CLERICAL_MISMATCH


def test_clerical_mismatch_on_letter_o_for_zero():
    result = _only(match_invoices([_row("INV-O12")], [_row("INV-012")]))
    assert result.status == InvoiceMatchStatus.CLERICAL_MISMATCH


def test_clerical_mismatch_on_case_and_spacing():
    result = _only(match_invoices([_row("inv 1023")], [_row("INV-1023")]))
    # Case/space-only differences are handled by the exact-match key.
    assert result.status == InvoiceMatchStatus.EXACT_MATCH


def test_clerical_reason_quotes_both_spellings():
    result = _only(match_invoices([_row("INV-007")], [_row("INV-7")]))
    assert "INV-007" in result.match_reason and "INV-7" in result.match_reason


# --- Tier 3: AMOUNT_MISMATCH ------------------------------------------------------

def test_amount_mismatch_when_tax_differs():
    ledger = [_row("INV-1", tax="1000.00")]
    gstr2b = [_row("INV-1", tax="5000.00")]
    result = _only(match_invoices(ledger, gstr2b))
    assert result.status == InvoiceMatchStatus.AMOUNT_MISMATCH


def test_amount_mismatch_reason_states_the_delta():
    ledger = [_row("INV-1", tax="1000.00")]
    gstr2b = [_row("INV-1", tax="5000.00")]
    assert "₹4,000.00" in _only(match_invoices(ledger, gstr2b)).match_reason


def test_amount_mismatch_keeps_both_sides_for_the_diff():
    result = _only(match_invoices([_row("INV-1", tax="1000.00")], [_row("INV-1", tax="5000.00")]))
    assert result.ledger_row is not None and result.gstr2b_row is not None


# --- Tier 4: MISSING_IN_GSTR2B (the one that matters) -----------------------------

def test_missing_in_gstr2b_when_supplier_has_not_filed():
    result = _only(match_invoices([_row("INV-1")], []))
    assert result.status == InvoiceMatchStatus.MISSING_IN_GSTR2B
    assert result.gstr2b_row is None


def test_missing_in_gstr2b_reason_names_the_vendor_and_the_amount():
    result = _only(match_invoices([_row("INV-1", tax="1840.00")], []))
    assert "Sharma Traders" in result.match_reason
    assert "₹1,840.00" in result.match_reason


def test_different_gstin_is_not_a_match():
    results = match_invoices([_row("INV-1", gstin=GSTIN_A)], [_row("INV-1", gstin=GSTIN_B)])
    statuses = {r.status for r in results}
    assert statuses == {
        InvoiceMatchStatus.MISSING_IN_GSTR2B, InvoiceMatchStatus.MISSING_IN_LEDGER
    }


# --- Tier 5: MISSING_IN_LEDGER ----------------------------------------------------

def test_missing_in_ledger_for_unrecorded_2b_entry():
    result = _only(match_invoices([], [_row("INV-9")]))
    assert result.status == InvoiceMatchStatus.MISSING_IN_LEDGER
    assert result.ledger_row is None


# --- Tier 6: ITC_INELIGIBLE -------------------------------------------------------

def test_itc_ineligible_when_2b_says_unavailable():
    gstr2b = [_row("INV-1", itc_available=False, itc_reason="POS and supplier state are same")]
    result = _only(match_invoices([_row("INV-1")], gstr2b))
    assert result.status == InvoiceMatchStatus.ITC_INELIGIBLE
    assert "POS and supplier state are same" in result.match_reason


def test_itc_ineligible_beats_exact_match():
    """A matched-but-unclaimable invoice is not a clean match — it must not inflate ITC."""
    gstr2b = [_row("INV-1", itc_available=False)]
    assert _only(match_invoices([_row("INV-1")], gstr2b)).status != InvoiceMatchStatus.EXACT_MATCH


def test_itc_available_true_still_matches_normally():
    gstr2b = [_row("INV-1", itc_available=True)]
    assert _only(match_invoices([_row("INV-1")], gstr2b)).status == InvoiceMatchStatus.EXACT_MATCH


# --- Tier 7: DUPLICATE ------------------------------------------------------------

def test_duplicate_invoice_number_for_one_vendor():
    ledger = [_row("INV-1"), _row("INV-1")]
    results = match_invoices(ledger, [_row("INV-1")])
    statuses = sorted(r.status for r in results)
    assert statuses == sorted([InvoiceMatchStatus.EXACT_MATCH, InvoiceMatchStatus.DUPLICATE])


def test_same_invoice_number_for_different_vendors_is_not_a_duplicate():
    ledger = [_row("INV-1", gstin=GSTIN_A), _row("INV-1", gstin=GSTIN_B)]
    results = match_invoices(ledger, [])
    assert all(r.status == InvoiceMatchStatus.MISSING_IN_GSTR2B for r in results)


# --- Invariants -------------------------------------------------------------------

def test_each_gstr2b_row_is_consumed_at_most_once():
    ledger = [_row("INV-1"), _row("INV-2")]
    results = match_invoices(ledger, [_row("INV-1")])
    statuses = sorted(r.status for r in results)
    assert statuses == sorted(
        [InvoiceMatchStatus.EXACT_MATCH, InvoiceMatchStatus.MISSING_IN_GSTR2B]
    )


def test_every_row_on_both_sides_is_accounted_for():
    ledger = [_row("INV-1"), _row("INV-2"), _row("INV-3")]
    gstr2b = [_row("INV-1"), _row("INV-9")]
    results = match_invoices(ledger, gstr2b)
    seen_ledger = {r.ledger_row.invoice_number for r in results if r.ledger_row}
    seen_2b = {r.gstr2b_row.invoice_number for r in results if r.gstr2b_row}
    assert seen_ledger == {"INV-1", "INV-2", "INV-3"}
    assert seen_2b == {"INV-1", "INV-9"}


def test_every_result_carries_a_human_readable_reason():
    results = match_invoices([_row("INV-1"), _row("INV-2")], [_row("INV-1"), _row("INV-9")])
    for result in results:
        assert result.match_reason and result.match_reason.endswith(".")
        assert result.match_reason[0].isupper()
