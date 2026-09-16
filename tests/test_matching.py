from datetime import date

from lockstep.models.enums import InvoiceMatchStatus
from lockstep.services.ingestion import CanonicalRow, normalize_gstin, normalize_invoice_number
from lockstep.services.matching import match_invoices


def _row(
    invoice_number: str, gstin: str, amount: float, vendor_name: str = "Vendor"
) -> CanonicalRow:
    return CanonicalRow(
        row_index=0,
        invoice_number=invoice_number,
        invoice_number_normalized=normalize_invoice_number(invoice_number),
        gstin=gstin,
        gstin_normalized=normalize_gstin(gstin),
        vendor_name=vendor_name,
        taxable_value=amount,
        itc_amount=amount,
        invoice_date=date(2025, 5, 1),
        description="",
        raw={"Invoice No": invoice_number},
    )


def test_exact_match():
    ledger = [_row("INV-1", "29ABCDE1234F1Z5", 1000.0)]
    gstr2b = [_row("INV-1", "29ABCDE1234F1Z5", 1000.0)]

    results = match_invoices(ledger, gstr2b)

    assert len(results) == 1
    assert results[0].status == InvoiceMatchStatus.EXACT_MATCH


def test_exact_match_tolerates_tiny_amount_rounding():
    ledger = [_row("INV-1", "29ABCDE1234F1Z5", 1000.40)]
    gstr2b = [_row("INV-1", "29ABCDE1234F1Z5", 1000.00)]

    results = match_invoices(ledger, gstr2b)

    assert results[0].status == InvoiceMatchStatus.EXACT_MATCH


def test_clerical_mismatch_on_invoice_number_typo():
    ledger = [_row("INV-2305", "27AAAAA0000A1Z1", 38900.0)]
    gstr2b = [_row("INV-2305A", "27AAAAA0000A1Z1", 38900.0)]

    results = match_invoices(ledger, gstr2b)

    assert results[0].status == InvoiceMatchStatus.CLERICAL_MISMATCH


def test_missing_in_gstr2b_when_vendor_never_filed():
    ledger = [_row("INV-1", "29ABCDE1234F1Z5", 1000.0)]
    gstr2b: list[CanonicalRow] = []

    results = match_invoices(ledger, gstr2b)

    assert results[0].status == InvoiceMatchStatus.MISSING_IN_GSTR2B
    assert results[0].gstr2b_row is None


def test_missing_in_ledger_for_unclaimed_2b_entry():
    ledger: list[CanonicalRow] = []
    gstr2b = [_row("INV-9", "29ZZZZZ9999Z1Z9", 500.0)]

    results = match_invoices(ledger, gstr2b)

    assert results[0].status == InvoiceMatchStatus.MISSING_IN_LEDGER
    assert results[0].ledger_row is None


def test_large_amount_mismatch_does_not_exact_match():
    ledger = [_row("INV-1", "29ABCDE1234F1Z5", 1000.0)]
    gstr2b = [_row("INV-1", "29ABCDE1234F1Z5", 5000.0)]

    results = match_invoices(ledger, gstr2b)

    # Same invoice number/GSTIN but wildly different amount should not silently exact-match
    assert results[0].status != InvoiceMatchStatus.EXACT_MATCH


def test_each_gstr2b_row_consumed_at_most_once():
    ledger = [_row("INV-1", "29ABCDE1234F1Z5", 1000.0), _row("INV-2", "29ABCDE1234F1Z5", 1000.0)]
    gstr2b = [_row("INV-1", "29ABCDE1234F1Z5", 1000.0)]

    results = match_invoices(ledger, gstr2b)
    statuses = sorted(r.status for r in results)
    expected = sorted([InvoiceMatchStatus.EXACT_MATCH, InvoiceMatchStatus.MISSING_IN_GSTR2B])

    assert statuses == expected
