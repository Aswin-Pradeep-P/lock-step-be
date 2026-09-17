"""The two demo GSTR-2B variants.

These assert the shape of the *narrative*: the second fetch must carry more invoices
than the first, because nine suppliers filed in between. If that stops being true the
period view's "9 filed since the last check" line is fiction.
"""

import pytest

from lockstep.core.exceptions import ValidationError
from lockstep.services.gsp import parse_gsp_gstr2b_response
from lockstep.services.mock_gstr2b import (
    defects_for,
    get_gstr2b_mock,
    get_gstr2b_payload,
    parse_variant,
)

RECOVERED_COUNT = 9


def _numbers(variant: str) -> set[str]:
    return {r.invoice_number for r in parse_gsp_gstr2b_response(get_gstr2b_payload(variant))}


def test_corrected_carries_more_invoices_than_inconsistent():
    """The whole reason two variants exist. Nine non-filers file between the fetches."""
    first = _numbers("inconsistent")
    second = _numbers("corrected")

    assert len(first) == 66
    assert len(second) == 75
    assert len(second) - len(first) == RECOVERED_COUNT


def test_the_recovered_invoices_are_purely_additive():
    """`corrected` must not quietly drop or renumber anything — the only difference is
    the nine that arrived. Otherwise the delta stops meaning 'suppliers filed'."""
    first = _numbers("inconsistent")
    second = _numbers("corrected")

    assert first < second
    assert len(second - first) == RECOVERED_COUNT


def test_recovered_invoices_are_filed_after_the_cutoff():
    """They filed late — that is why they were missing at the first fetch, and why a
    chase call was needed. A recovered invoice filed before the 13th would make no sense."""
    first = _numbers("inconsistent")
    rows = parse_gsp_gstr2b_response(get_gstr2b_payload("corrected"))
    recovered = [r for r in rows if r.invoice_number not in first]

    assert len(recovered) == RECOVERED_COUNT
    for row in recovered:
        assert row.supplier_filed_at is not None
        # Period 112024's cutoff is 13-12-2024.
        assert row.supplier_filed_at.day > 13


def test_declared_defects_match_what_the_matcher_actually_produces():
    """The counters used to be `len()` arithmetic over hardcoded constants, and they
    were wrong — a clerical rewrite that did not fold was reported as a clerical for
    months. These numbers are now measured by scripts/generate_demo_dataset.py."""
    assert defects_for("inconsistent") == {
        "exact": 50, "clerical": 7, "amount_mismatch": 5,
        "missing_in_2b": 14, "itc_ineligible": 4,
    }
    assert defects_for("corrected") == {
        "exact": 59, "clerical": 7, "amount_mismatch": 5,
        "missing_in_2b": 5, "itc_ineligible": 4,
    }


def test_defect_totals_agree_with_the_register_size():
    """Every one of the 80 register invoices lands in exactly one bucket."""
    for variant in ("inconsistent", "corrected"):
        assert sum(defects_for(variant).values()) == 80


def test_itc_blocks_survive_into_the_corrected_fetch():
    """An ITC block is a portal ruling, not a supplier oversight — it does not clear
    just because time passed."""
    for variant in ("inconsistent", "corrected"):
        rows = parse_gsp_gstr2b_response(get_gstr2b_payload(variant))
        blocked = [r for r in rows if r.itc_available is False]
        assert len(blocked) == 4
        assert all(r.itc_reason for r in blocked), "a block with no reason is unactionable"


def test_mock_envelope_reports_variant_count_and_defects():
    mock = get_gstr2b_mock("inconsistent")
    assert mock["variant"] == "inconsistent"
    assert mock["count"] == 66
    assert mock["defects"] == defects_for("inconsistent")
    assert mock["data"]["data"]["data"]["data"]["docdata"]["b2b"]


def test_payload_is_a_copy_so_callers_cannot_poison_the_cache():
    first = get_gstr2b_payload("corrected")
    first["data"]["data"]["data"]["docdata"]["b2b"].clear()
    assert get_gstr2b_payload("corrected")["data"]["data"]["data"]["docdata"]["b2b"]


def test_parse_variant_defaults_and_rejects_unknown():
    assert parse_variant(None) == "corrected"
    assert parse_variant(" Inconsistent ") == "inconsistent"
    with pytest.raises(ValidationError):
        parse_variant("optimistic")
