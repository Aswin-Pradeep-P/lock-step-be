from datetime import date

from lockstep.services.risk_rules import (
    compute_recoverable_until,
    days_remaining,
    financial_year_end,
    is_window_open,
    sec17_5_hint,
)


def test_financial_year_end_for_month_in_first_half():
    # Jan-Mar falls in the FY that started the previous April
    assert financial_year_end(date(2025, 2, 15)) == 2025


def test_financial_year_end_for_month_in_second_half():
    # April onwards starts a new FY, ending the following calendar year
    assert financial_year_end(date(2025, 5, 1)) == 2026


def test_recoverable_until_is_30_november_of_fy_end_year():
    deadline = compute_recoverable_until(date(2025, 5, 1))
    assert deadline == date(2026, 11, 30)


def test_days_remaining_positive_before_deadline():
    deadline = date(2026, 11, 30)
    assert days_remaining(deadline, today=date(2026, 1, 1)) > 0


def test_window_closed_after_deadline():
    deadline = date(2025, 11, 30)
    assert is_window_open(deadline, today=date(2026, 1, 1)) is False


def test_sec17_5_hint_matches_known_ineligible_category():
    assert sec17_5_hint("Outdoor catering for annual event") is True


def test_sec17_5_hint_does_not_flag_ordinary_purchase():
    assert sec17_5_hint("Steel rods for factory floor") is False


def test_sec17_5_hint_handles_empty_description():
    assert sec17_5_hint("") is False
