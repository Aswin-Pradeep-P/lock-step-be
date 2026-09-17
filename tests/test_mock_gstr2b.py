from lockstep.services.gsp import parse_gsp_gstr2b_response
from lockstep.services.mock_gstr2b import (
    CLERICAL_INVOICES,
    CORRECTED_ITC_BLOCKED,
    CORRECTED_TAX_SCALE,
    ITC_BLOCKED,
    OMITTED_INVOICES,
    get_gstr2b_mock,
    get_gstr2b_payload,
    parse_variant,
)
from lockstep.core.exceptions import ValidationError

import pytest


def test_corrected_payload_has_mild_defects():
    """Corrected variant keeps all 13 invoices but applies mild mutations."""
    payload = get_gstr2b_payload("corrected")
    rows = parse_gsp_gstr2b_response(payload)
    assert len(rows) == 13

    # One invoice has a +1% tax bump
    scaled = next(r for r in rows if r.invoice_number == "AIN2425002589127")
    # original igst was 35686.04; 1% up is 36042.90
    assert str(scaled.igst) == "36042.90"

    # One invoice has ITC blocked
    blocked = [r for r in rows if r.invoice_number in CORRECTED_ITC_BLOCKED]
    assert len(blocked) == len(CORRECTED_ITC_BLOCKED)
    assert all(r.itc_available is False for r in blocked)

    # No invoice numbers are changed or omitted
    numbers = {r.invoice_number for r in rows}
    assert not OMITTED_INVOICES.isdisjoint(numbers)  # omitted invoices still present


def test_inconsistent_omits_unfiled_invoices_and_seeds_defects():
    payload = get_gstr2b_payload("inconsistent")
    rows = parse_gsp_gstr2b_response(payload)
    numbers = {r.invoice_number for r in rows}

    assert OMITTED_INVOICES.isdisjoint(numbers)
    assert set(CLERICAL_INVOICES.values()) <= numbers
    for original in CLERICAL_INVOICES:
        assert original not in numbers

    scaled = next(r for r in rows if r.invoice_number == "AIN2425002587878")
    assert scaled.igst != scaled.taxable_value  # still has tax
    # original igst was 12461.39; 3% up is 12835.23
    assert str(scaled.igst) == "12835.23"

    blocked = [r for r in rows if r.invoice_number in ITC_BLOCKED]
    assert len(blocked) == len(ITC_BLOCKED)
    assert all(r.itc_available is False for r in blocked)


def test_mock_envelope_reports_counts():
    inconsistent = get_gstr2b_mock("inconsistent")
    corrected = get_gstr2b_mock("corrected")
    assert inconsistent["count"] == 11
    assert inconsistent["defects"]["missing_in_2b"] == 2
    assert corrected["count"] == 13
    assert corrected["defects"]["exact"] == 11
    assert corrected["defects"]["amount_mismatch"] == 1
    assert corrected["defects"]["itc_ineligible"] == 1
    assert corrected["defects"]["missing_in_2b"] == 0
    assert corrected["defects"]["clerical"] == 0


def test_parse_variant_rejects_unknown():
    with pytest.raises(ValidationError):
        parse_variant("random")
    assert parse_variant(None) == "corrected"
