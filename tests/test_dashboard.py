"""corrections_for_check — keyed recovery vs the lineage baseline check.

No live DB: FakeDB queues the two query results the function issues, so the
suite stays pure like the rest of tests/.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from lockstep.models.enums import InvoiceMatchStatus, InvoiceSource
from lockstep.services.dashboard import corrections_for_check

GSTIN = "29AAAAA0000A1Z5"
SEED_GSTIN = "27BBBBB0000B1Z5"


def _inv(
    check_id,
    number: str,
    status: InvoiceMatchStatus,
    tax: str,
    *,
    gstin: str = GSTIN,
    source: InvoiceSource = InvoiceSource.LEDGER,
) -> SimpleNamespace:
    amount = Decimal(tax)
    return SimpleNamespace(
        check_id=check_id,
        vendor_gstin=gstin,
        invoice_number=number,
        status=status,
        source=source,
        igst=amount,
        cgst=Decimal("0"),
        sgst=Decimal("0"),
        cess=Decimal("0"),
        total_tax=amount,
    )


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, *, scalars=None):
        self._scalars = scalars if scalars is not None else []

    def scalars(self):
        return _Scalars(self._scalars)


class FakeDB:
    """Queues results for the queries `corrections_for_check` issues in order."""

    def __init__(self, results: list[_Result]):
        self._results = list(results)

    async def execute(self, _stmt):
        return self._results.pop(0)


def _db(completed_ids, ledger_rows) -> FakeDB:
    return FakeDB(
        [
            _Result(scalars=completed_ids),
            _Result(scalars=ledger_rows),
        ]
    )


@pytest.fixture
def correction_scenario():
    """Three checks over the same three ledger invoices; then a 2B-only re-run.

    Check 1 — all MISSING_IN_GSTR2B (baseline).
    Check 2 — INV-001 and INV-002 become EXACT_MATCH.
    Check 3 — INV-003 also EXACT_MATCH (cumulative vs check 1, not vs check 2).
    Check 2B — no ledger rows in the viewed set.
    """
    period_id = uuid4()
    check1, check2, check3, check_2b = uuid4(), uuid4(), uuid4(), uuid4()
    taxes = {"INV-001": "100.00", "INV-002": "200.00", "INV-003": "300.00"}
    numbers = list(taxes)

    check1_rows = [
        _inv(check1, n, InvoiceMatchStatus.MISSING_IN_GSTR2B, taxes[n]) for n in numbers
    ]
    check2_rows = [
        _inv(check2, numbers[0], InvoiceMatchStatus.EXACT_MATCH, taxes[numbers[0]]),
        _inv(check2, numbers[1], InvoiceMatchStatus.EXACT_MATCH, taxes[numbers[1]]),
        _inv(check2, numbers[2], InvoiceMatchStatus.MISSING_IN_GSTR2B, taxes[numbers[2]]),
    ]
    check3_rows = [
        _inv(check3, n, InvoiceMatchStatus.EXACT_MATCH, taxes[n]) for n in numbers
    ]

    return {
        "period_id": period_id,
        "check1": check1,
        "check2": check2,
        "check3": check3,
        "check_2b": check_2b,
        "completed": [check1, check2, check3, check_2b],
        "all_ledger": check1_rows + check2_rows + check3_rows,
        "tax_001_002": Decimal("100.00") + Decimal("200.00"),
        "tax_all": Decimal("100.00") + Decimal("200.00") + Decimal("300.00"),
    }


async def test_first_check_is_zero(correction_scenario):
    s = correction_scenario
    db = _db(s["completed"], s["all_ledger"])
    count, saved = await corrections_for_check(db, s["period_id"], s["check1"])
    assert count == 0
    assert saved == Decimal(0)


async def test_second_check_reports_what_that_rerun_fixed(correction_scenario):
    s = correction_scenario
    db = _db(s["completed"], s["all_ledger"])
    count, saved = await corrections_for_check(db, s["period_id"], s["check2"])
    assert count == 2
    assert saved == s["tax_001_002"]


async def test_third_check_accumulates_against_first(correction_scenario):
    s = correction_scenario
    db = _db(s["completed"], s["all_ledger"])
    count, saved = await corrections_for_check(db, s["period_id"], s["check3"])
    assert count == 3
    assert saved == s["tax_all"]


async def test_twob_only_rerun_reports_zero_not_everything(correction_scenario):
    """Absent ledger keys do not count — a raw count diff would say everything fixed."""
    s = correction_scenario
    db = _db(s["completed"], s["all_ledger"])  # check_2b has no ledger rows
    count, saved = await corrections_for_check(db, s["period_id"], s["check_2b"])
    assert count == 0
    assert saved == Decimal(0)


async def test_unrelated_prior_check_is_not_baseline():
    """Seed (or any prior upload with different keys) must not zero out a later lineage."""
    period_id = uuid4()
    seed, run1, run2 = uuid4(), uuid4(), uuid4()

    seed_rows = [
        _inv(seed, "SEED-1", InvoiceMatchStatus.MISSING_IN_GSTR2B, "999.00", gstin=SEED_GSTIN),
    ]
    run1_rows = [
        _inv(run1, "INV-001", InvoiceMatchStatus.MISSING_IN_GSTR2B, "100.00"),
        _inv(run1, "INV-002", InvoiceMatchStatus.MISSING_IN_GSTR2B, "200.00"),
    ]
    run2_rows = [
        _inv(run2, "INV-001", InvoiceMatchStatus.EXACT_MATCH, "100.00"),
        _inv(run2, "INV-002", InvoiceMatchStatus.EXACT_MATCH, "200.00"),
    ]

    db = _db([seed, run1, run2], seed_rows + run1_rows + run2_rows)
    count, saved = await corrections_for_check(db, period_id, run2)
    assert count == 2
    assert saved == Decimal("300.00")
