import json
from pathlib import Path

from lockstep.services.gsp import parse_gsp_gstr2b_response
from lockstep.services.mock_gstr2b import (
    CLERICAL_INVOICES,
    ITC_BLOCKED,
    OMITTED_INVOICES,
    get_gstr2b_mock,
    get_gstr2b_payload,
    parse_variant,
)
from lockstep.core.exceptions import ValidationError

import pytest


def test_corrected_payload_is_the_sample_fixture():
    """Existing GSP fetch with no variant must keep returning the original sample."""
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "gsp_gstr2b_sample.json").read_text()
    )
    assert get_gstr2b_payload("corrected") == fixture
    assert get_gstr2b_payload() == fixture


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
    assert corrected["defects"]["exact"] == 13


def test_parse_variant_rejects_unknown():
    with pytest.raises(ValidationError):
        parse_variant("random")
    assert parse_variant(None) == "corrected"
