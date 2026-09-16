"""Verified against the actual GSP sandbox payload shape (Sandbox.co.in-style
GSTR-2B response), not a guessed one — see tests/fixtures/gsp_gstr2b_sample.json."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from lockstep.core.exceptions import IngestionError
from lockstep.services.gsp import parse_gsp_gstr2b_response

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "gsp_gstr2b_sample.json").read_text()
)


def test_parses_every_invoice_across_every_vendor():
    rows = parse_gsp_gstr2b_response(FIXTURE)
    # 3 + 2 + 1*8 = 13 invoices across 10 vendors in the sample.
    assert len(rows) == 13
    assert len({r.gstin_normalized for r in rows}) == 10


def test_amazon_invoice_fields_map_correctly():
    rows = parse_gsp_gstr2b_response(FIXTURE)
    aws = next(r for r in rows if r.invoice_number == "AIN2425002587878")

    assert aws.gstin_normalized == "07AAJCA9880A1ZL"
    assert aws.vendor_name == "AMAZON WEB SERVICES INDIA PRIVATE LIMITED"
    assert aws.taxable_value == Decimal("69229.92")
    assert aws.igst == Decimal("12461.39")
    assert aws.cgst == Decimal("0.00")
    assert aws.invoice_date.isoformat() == "2024-11-02"
    assert aws.supplier_filed_at.isoformat() == "2024-11-10"
    assert aws.itc_available is True
    assert aws.is_reverse_charge is False


def test_intrastate_invoice_splits_cgst_sgst():
    rows = parse_gsp_gstr2b_response(FIXTURE)
    jio = next(r for r in rows if r.invoice_number == "C24E242500023146")

    assert jio.igst == Decimal("0.00")
    assert jio.cgst == Decimal("432.11")
    assert jio.sgst == Decimal("432.11")
    assert jio.total_tax == Decimal("864.22")


def test_raw_data_preserves_ims_status_and_irn():
    """Not surfaced into matching yet (see gsp.py), but never silently dropped —
    a CA inspecting raw_data can still see it."""
    rows = parse_gsp_gstr2b_response(FIXTURE)
    itech = next(r for r in rows if r.invoice_number == "IT/24-25/978")

    assert itech.raw["inv"]["imsStatus"] == "N"
    assert itech.raw["inv"]["irn"].startswith("aa77ad30")


def test_clerical_key_and_normalization_are_reused_from_ingestion():
    """The GSP path must behave identically to the file-upload path for matching —
    same normalization, same tiers, same tests already proving those tiers work."""
    rows = parse_gsp_gstr2b_response(FIXTURE)
    zoho = next(r for r in rows if r.invoice_number == "ZOHO-INV-24112")
    assert zoho.invoice_number_normalized == "ZOHOINV24112"
    assert zoho.clerical_key  # non-empty; exact value is ingestion's own concern


def test_rejects_a_response_missing_the_expected_envelope():
    with pytest.raises(IngestionError):
        parse_gsp_gstr2b_response({"code": 200, "data": {}})


def test_empty_b2b_list_yields_no_rows():
    payload = {"data": {"data": {"data": {"docdata": {"b2b": []}}}}}
    assert parse_gsp_gstr2b_response(payload) == []
