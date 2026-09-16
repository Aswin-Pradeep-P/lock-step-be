"""Regression tests from the real sample exports in ../lock-step-fe/samples.

Each of these was a live bug found by feeding the actual files through the pipeline —
header normalisation is a real task, not an afterthought.
"""

from datetime import date
from decimal import Decimal

from lockstep.models.enums import InvoiceMatchStatus
from lockstep.services.ingestion import (
    normalize_name,
    parse_csv,
    resolve_columns,
    to_canonical_rows,
)
from lockstep.services.matching import match_invoices, resolve_missing_gstins

TALLY_HEADER = (
    "Date,Particulars,Buyer/Supplier,Voucher Type,Voucher No.,Voucher Ref. No.,"
    "Voucher Ref. Date,Narration,Gross Total,IGST Input,CGST Input,SGST Input"
)
TALLY_ROW = (
    "2024-08-01,Server Hosting,CloudNine Technologies Pvt Ltd,Purchase,PV-CN-001,"
    "CN/INV/2024/801,2024-07-28,Annual cloud hosting renewal,236000,36000,0,0"
)
GSTR2B_HEADER = (
    "GSTIN of supplier,Trade/Legal name,Invoice number,Invoice type,Invoice Date,"
    "Invoice Value(₹),Place of supply,Supply Attract Reverse Charge,Taxable Value (₹),"
    "Integrated Tax(₹),Central Tax(₹),State/UT Tax(₹),Cess(₹),GSTR-1/IFF/GSTR-5 Period,"
    "GSTR-1/IFF/GSTR-5 Filing Date,ITC Availability,Reason,Applicable % of Tax Rate,"
    "Source,IRN,IRN Date"
)
GSTR2B_ROW = (
    "29AAACN5678P1Z3,CloudNine Technologies Pvt Ltd,CN/INV/2024/801,Regular,2024-07-28,"
    "236000,29-Karnataka,N,200000,36000,0,0,0,082024,2024-09-11,Yes,,18%,GSTR-1,,"
)


def _rows(header: str, *data: str):
    content = ("\n".join([header, *data]) + "\n").encode()
    parsed, mapping = parse_csv(content)
    return to_canonical_rows(parsed, mapping), mapping


def test_supplier_reference_wins_over_our_internal_voucher_number():
    """'Voucher No.' is our own PV-CN-001. GSTR-2B has only the supplier's
    'Voucher Ref. No.', so keying on the wrong one matches nothing."""
    resolved = resolve_columns(TALLY_HEADER.split(","))
    assert resolved["invoice_number"] == "Voucher Ref. No."


def test_supplier_invoice_date_wins_over_our_booking_date():
    """Booked 01 Aug, invoiced 28 Jul — 4 days apart, outside the ±3 day tolerance."""
    resolved = resolve_columns(TALLY_HEADER.split(","))
    assert resolved["invoice_date"] == "Voucher Ref. Date"


def test_tally_export_parses_without_a_gstin_column():
    """A purchase register usually has a party name and no GSTIN at all."""
    rows, mapping = _rows(TALLY_HEADER, TALLY_ROW)
    assert "gstin" not in mapping
    assert rows[0].invoice_number == "CN/INV/2024/801"
    assert rows[0].invoice_date == date(2024, 7, 28)
    assert rows[0].igst == Decimal("36000.00")
    assert rows[0].vendor_name == "CloudNine Technologies Pvt Ltd"


def test_gstr2b_export_keeps_filing_date_and_rupee_symbol_headers():
    rows, mapping = _rows(GSTR2B_HEADER, GSTR2B_ROW)
    assert mapping["filing_date"] == "GSTR-1/IFF/GSTR-5 Filing Date"
    assert rows[0].supplier_filed_at == date(2024, 9, 11)
    assert rows[0].igst == Decimal("36000.00")
    assert rows[0].itc_available is True


def test_normalize_name_folds_company_suffixes():
    assert normalize_name("CloudNine Technologies Pvt Ltd") == normalize_name(
        "CLOUDNINE TECHNOLOGIES"
    )
    assert normalize_name("Sharma Traders") != normalize_name("Sharma Steel")


def test_gstin_is_resolved_from_the_party_name():
    ledger, _ = _rows(TALLY_HEADER, TALLY_ROW)
    gstr2b, _ = _rows(GSTR2B_HEADER, GSTR2B_ROW)

    assert ledger[0].gstin_normalized == ""
    assert resolve_missing_gstins(ledger, gstr2b) == 1
    assert ledger[0].gstin_normalized == "29AAACN5678P1Z3"


def test_unknown_party_name_is_left_blank_never_guessed():
    unknown = TALLY_ROW.replace("CloudNine Technologies Pvt Ltd", "Someone Else")
    ledger, _ = _rows(TALLY_HEADER, unknown)
    gstr2b, _ = _rows(GSTR2B_HEADER, GSTR2B_ROW)

    assert resolve_missing_gstins(ledger, gstr2b) == 0
    assert ledger[0].gstin_normalized == ""


def test_real_sample_pair_reconciles_to_an_exact_match():
    """End to end: the two shipped sample files must agree on this invoice."""
    ledger, _ = _rows(TALLY_HEADER, TALLY_ROW)
    gstr2b, _ = _rows(GSTR2B_HEADER, GSTR2B_ROW)
    resolve_missing_gstins(ledger, gstr2b)

    results = match_invoices(ledger, gstr2b)
    assert len(results) == 1
    assert results[0].status == InvoiceMatchStatus.EXACT_MATCH
