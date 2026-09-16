"""The 13th is the only date that decides anything. These tests pin it down."""

from datetime import date

import pytest

from lockstep.services.periods import (
    days_past_cutoff,
    days_to_cutoff,
    filed_on_time,
    format_tax_period,
    parse_tax_period,
    period_dates,
    previous_tax_period,
    tax_period_for,
)


def test_parse_tax_period():
    assert parse_tax_period("082026") == (8, 2026)


@pytest.mark.parametrize("bad", ["8-2026", "132026", "", "2026", "AA2026"])
def test_parse_tax_period_rejects_malformed_input(bad):
    with pytest.raises(ValueError):
        parse_tax_period(bad)


def test_format_and_round_trip():
    assert format_tax_period(8, 2026) == "082026"
    assert tax_period_for(date(2026, 8, 31)) == "082026"


def test_previous_tax_period_crosses_the_year_boundary():
    assert previous_tax_period("012026") == "122025"
    assert previous_tax_period("082026") == "072026"


def test_period_dates_fall_in_the_month_after_the_period():
    dates = period_dates("082026")
    assert dates["gstr1_due"] == date(2026, 9, 11)
    assert dates["cutoff_date"] == date(2026, 9, 13)
    assert dates["gstr2b_date"] == date(2026, 9, 14)
    assert dates["filing_due"] == date(2026, 9, 20)


def test_period_dates_roll_december_into_january():
    assert period_dates("122026")["cutoff_date"] == date(2027, 1, 13)


def test_days_to_cutoff_counts_down_inside_the_window():
    # The product's whole claim: on the 7th there are still 6 days to act.
    assert days_to_cutoff("082026", today=date(2026, 9, 7)) == 6


def test_days_to_cutoff_goes_negative_once_the_window_closes():
    assert days_to_cutoff("082026", today=date(2026, 9, 15)) == -2


def test_days_past_cutoff_is_negative_when_filed_early():
    assert days_past_cutoff("082026", date(2026, 9, 10)) == -3


def test_days_past_cutoff_is_positive_when_filed_late():
    assert days_past_cutoff("082026", date(2026, 9, 17)) == 4


def test_days_past_cutoff_is_none_when_never_filed():
    assert days_past_cutoff("082026", None) is None


def test_filed_on_time_is_inclusive_of_the_thirteenth():
    assert filed_on_time("082026", date(2026, 9, 13)) is True
    assert filed_on_time("082026", date(2026, 9, 14)) is False
    assert filed_on_time("082026", None) is False


def test_tax_period_sort_key_orders_chronologically():
    """MMYYYY sorted as a plain string orders by month: '082024' would sit between
    '092026' and '072026'. Year has to come first."""
    periods = ["092026", "082024", "072026", "122025"]
    ordered = sorted(periods, key=lambda p: (p[2:], p[:2]))
    assert ordered == ["082024", "122025", "072026", "092026"]
