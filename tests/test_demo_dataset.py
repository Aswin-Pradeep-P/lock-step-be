"""The shipped demo pair must reconcile to the mix it claims.

This is the guard the old dataset lacked. Its defect counters were `len()` over
hardcoded constants — a claim, never a measurement — and one of the claims was false
for months: a clerical rewrite used a `2 -> Z` swap that `clerical_key` does not fold,
so the row fell out as MISSING on both sides while the counter still said "clerical".

Here the register and both GSTR-2B payloads are read from disk and put through the real
ingestion, GSP parsing and matching code. If someone edits a fixture by hand, or
regenerates without re-checking, these fail loudly.
"""

from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from lockstep.services.gsp import parse_gsp_gstr2b_response
from lockstep.services.ingestion import parse_file, to_canonical_rows
from lockstep.services.matching import match_invoices, resolve_missing_gstins
from lockstep.services.mock_gstr2b import defects_for, get_gstr2b_payload

REGISTER = Path(__file__).parent / "fixtures" / "purchase_register_gsp.csv"
TOTAL_INVOICES = 80

EXPECTED = {
    "inconsistent": {
        "EXACT_MATCH": 50, "MISSING_IN_GSTR2B": 14, "CLERICAL_MISMATCH": 7,
        "AMOUNT_MISMATCH": 5, "ITC_INELIGIBLE": 4,
    },
    "corrected": {
        "EXACT_MATCH": 59, "MISSING_IN_GSTR2B": 5, "CLERICAL_MISMATCH": 7,
        "AMOUNT_MISMATCH": 5, "ITC_INELIGIBLE": 4,
    },
}


def _ledger():
    rows, mapping = parse_file(REGISTER.name, REGISTER.read_bytes(), hint="ledger")
    return to_canonical_rows(rows, mapping)


def _reconcile(variant: str):
    ledger = _ledger()
    two_b = parse_gsp_gstr2b_response(get_gstr2b_payload(variant))
    resolve_missing_gstins(ledger, two_b)
    return match_invoices(ledger, two_b)


@pytest.mark.parametrize("variant", ["inconsistent", "corrected"])
def test_reconciles_to_the_designed_status_mix(variant):
    results = _reconcile(variant)
    assert len(results) == TOTAL_INVOICES
    assert Counter(str(r.status) for r in results) == Counter(EXPECTED[variant])


@pytest.mark.parametrize("variant", ["inconsistent", "corrected"])
def test_declared_defects_match_the_matcher(variant):
    """`defects_for` is what the FE shows on the fetch panel. It has to be true."""
    results = _reconcile(variant)
    counts = Counter(str(r.status) for r in results)
    declared = defects_for(variant)
    assert declared["exact"] == counts["EXACT_MATCH"]
    assert declared["clerical"] == counts["CLERICAL_MISMATCH"]
    assert declared["amount_mismatch"] == counts["AMOUNT_MISMATCH"]
    assert declared["missing_in_2b"] == counts["MISSING_IN_GSTR2B"]
    assert declared["itc_ineligible"] == counts["ITC_INELIGIBLE"]


def test_nine_invoices_are_recovered_between_the_two_fetches():
    """The number the demo says out loud."""
    first = {
        r.ledger_row.invoice_number
        for r in _reconcile("inconsistent")
        if str(r.status) == "MISSING_IN_GSTR2B"
    }
    second = {
        r.ledger_row.invoice_number
        for r in _reconcile("corrected")
        if str(r.status) == "MISSING_IN_GSTR2B"
    }
    assert second < first
    assert len(first - second) == 9


def test_recovered_tax_is_worth_talking_about():
    """~Rs 16k recovered, and the residual is larger than what was recovered — the
    second screen should still open on an unresolved problem, not a victory lap."""
    def exposure(variant: str) -> Decimal:
        return sum(
            (r.ledger_row.total_tax for r in _reconcile(variant)
             if str(r.status) == "MISSING_IN_GSTR2B"),
            Decimal("0"),
        )

    before, after = exposure("inconsistent"), exposure("corrected")
    recovered = before - after
    assert Decimal("14000") <= recovered <= Decimal("18000")
    assert after > recovered


def test_total_tax_sits_in_the_demo_band():
    total = sum((row.total_tax for row in _ledger()), Decimal("0"))
    assert Decimal("200000") <= total <= Decimal("300000")


def test_no_duplicate_invoice_keys_in_the_register():
    """A repeated (GSTIN, invoice number) fires DUPLICATE before any matching runs and
    would silently eat a row out of whichever bucket it was meant to land in."""
    ledger = _ledger()
    keys = [(r.gstin_normalized, r.invoice_number_normalized) for r in ledger]
    assert len(set(keys)) == len(keys)


def test_every_result_carries_an_auditable_reason():
    for result in _reconcile("inconsistent"):
        assert result.match_reason, f"{result.status} with no reason is not auditable"


def test_fe_sample_copy_is_identical_when_present():
    """The FE repo ships its own copy for the upload demo; a drifted copy reconciles
    differently from what the tests prove."""
    fe_copy = Path(__file__).parents[2] / "lock-step-fe" / "samples" / "purchase_register_gsp.csv"
    if not fe_copy.exists():
        pytest.skip("frontend repo not checked out alongside")
    assert fe_copy.read_bytes() == REGISTER.read_bytes()
