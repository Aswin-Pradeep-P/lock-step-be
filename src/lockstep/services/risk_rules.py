"""Deterministic date/eligibility math that must never be left to the LLM.

The AI classifier is handed the outputs of this module as facts (deadline,
days remaining, whether a Sec 17(5)-blocked keyword was seen) — it explains
and cites, it does not compute dates.
"""

from __future__ import annotations

from datetime import date

# Sec 16(4): ITC for a financial year must be claimed by 30 Nov of the
# following FY (or the annual return filing date, if earlier — we don't have
# that date, so we use the statutory backstop).
SEC16_4_DEADLINE_MONTH = 11
SEC16_4_DEADLINE_DAY = 30

# Informational only — the AI classifier makes the final ineligibility call
# from the invoice description, this just seeds the prompt with a hint.
SEC17_5_HINT_KEYWORDS = [
    "catering", "food", "beverage", "outdoor catering", "canteen", "health service",
    "life insurance", "health insurance", "membership of a club", "club membership",
    "travel benefit", "leave travel", "motor vehicle", "works contract",
    "rent-a-cab", "cab service", "employee insurance",
]


def financial_year_end(invoice_date: date) -> int:
    """Return the calendar year in which the invoice's FY ends (e.g. 2025 for FY24-25)."""
    return invoice_date.year + 1 if invoice_date.month >= 4 else invoice_date.year


def compute_recoverable_until(invoice_date: date) -> date:
    fy_end_year = financial_year_end(invoice_date)
    return date(fy_end_year, SEC16_4_DEADLINE_MONTH, SEC16_4_DEADLINE_DAY)


def days_remaining(deadline: date, today: date | None = None) -> int:
    today = today or date.today()
    return (deadline - today).days


def is_window_open(deadline: date, today: date | None = None) -> bool:
    return days_remaining(deadline, today) > 0


def sec17_5_hint(description: str) -> bool:
    lowered = (description or "").lower()
    return any(keyword in lowered for keyword in SEC17_5_HINT_KEYWORDS)
