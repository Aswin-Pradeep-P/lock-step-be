from datetime import date

import pytest

from lockstep.core.exceptions import IngestionError
from lockstep.services.ingestion import (
    parse_amount,
    parse_csv,
    parse_date,
    resolve_columns,
    to_canonical_rows,
)


def test_resolve_columns_recognizes_tally_style_headers():
    columns = ["Invoice No", "Invoice Date", "Supplier GSTIN", "Supplier Name", "Taxable Value"]
    resolved = resolve_columns(columns)

    assert resolved["invoice_number"] == "Invoice No"
    assert resolved["gstin"] == "Supplier GSTIN"
    assert resolved["taxable_value"] == "Taxable Value"


def test_resolve_columns_recognizes_gstr2b_style_headers():
    columns = [
        "GSTIN of Supplier", "Trade/Legal Name", "Invoice Number", "Invoice Date", "Taxable Value",
    ]
    resolved = resolve_columns(columns)

    assert resolved["gstin"] == "GSTIN of Supplier"
    assert resolved["vendor_name"] == "Trade/Legal Name"
    assert resolved["invoice_number"] == "Invoice Number"


def test_parse_csv_raises_when_required_column_missing():
    content = b"Foo,Bar\n1,2\n"
    with pytest.raises(IngestionError):
        parse_csv(content)


def test_parse_csv_round_trips_rows():
    content = b"Invoice No,Supplier GSTIN,Taxable Value\nINV-1,29ABCDE1234F1Z5,1000\n"
    rows, columns = parse_csv(content)

    assert rows == [
        {"Invoice No": "INV-1", "Supplier GSTIN": "29ABCDE1234F1Z5", "Taxable Value": "1000"}
    ]
    assert columns["invoice_number"] == "Invoice No"


def test_parse_amount_strips_currency_and_commas():
    assert parse_amount("₹1,20,400.00") == 120400.0
    assert parse_amount("") == 0.0


def test_parse_date_handles_dd_mm_yyyy():
    assert parse_date("05-05-2025") == date(2025, 5, 5)


def test_to_canonical_rows_computes_itc_amount_from_tax_columns():
    content = (
        b"GSTIN of Supplier,Invoice Number,Taxable Value,Central Tax,State/UT Tax\n"
        b"29ABCDE1234F1Z5,INV-1,1000,90,90\n"
    )
    rows, columns = parse_csv(content)
    canonical = to_canonical_rows(rows, columns)

    assert canonical[0].itc_amount == 180.0
    assert canonical[0].taxable_value == 1000.0
