"""Tax-period arithmetic. `MMYYYY` in, IST calendar dates out.

The 13th is the only date that decides anything: supplier filings after it miss this
period's GSTR-2B, so the whole product lives in the 1st-to-13th window.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

CUTOFF_DAY = 13  # supplier filings after this miss this period's 2B
GSTR2B_DAY = 14  # 2B generated for the buyer
FILING_DUE_DAY = 20  # buyer's GSTR-3B
GSTR1_DUE_DAY = 11  # supplier's GSTR-1

IST = timezone(timedelta(hours=5, minutes=30))


def today_ist() -> date:
    return datetime.now(IST).date()


def parse_tax_period(tax_period: str) -> tuple[int, int]:
    """'082026' -> (8, 2026). Raises ValueError on anything else."""
    if len(tax_period) != 6 or not tax_period.isdigit():
        raise ValueError(f"tax_period must be MMYYYY, got {tax_period!r}")
    month, year = int(tax_period[:2]), int(tax_period[2:])
    if not 1 <= month <= 12:
        raise ValueError(f"tax_period month out of range: {tax_period!r}")
    return month, year


def format_tax_period(month: int, year: int) -> str:
    return f"{month:02d}{year:04d}"


def tax_period_for(day: date) -> str:
    return format_tax_period(day.month, day.year)


def previous_tax_period(tax_period: str) -> str:
    month, year = parse_tax_period(tax_period)
    return format_tax_period(12, year - 1) if month == 1 else format_tax_period(month - 1, year)


def period_dates(tax_period: str) -> dict[str, date]:
    """Key dates for a period. They fall in the month *after* the period being filed."""
    month, year = parse_tax_period(tax_period)
    filing_month, filing_year = (1, year + 1) if month == 12 else (month + 1, year)
    return {
        "gstr1_due": date(filing_year, filing_month, GSTR1_DUE_DAY),
        "cutoff_date": date(filing_year, filing_month, CUTOFF_DAY),
        "gstr2b_date": date(filing_year, filing_month, GSTR2B_DAY),
        "filing_due": date(filing_year, filing_month, FILING_DUE_DAY),
    }


def days_to_cutoff(tax_period: str, today: date | None = None) -> int:
    """Days remaining before the 13th. Negative once the window has closed."""
    return (period_dates(tax_period)["cutoff_date"] - (today or today_ist())).days


def days_past_cutoff(tax_period: str, filed_at: date | None) -> int | None:
    """Negative when the vendor filed before the 13th. None when they haven't filed."""
    if filed_at is None:
        return None
    return (filed_at - period_dates(tax_period)["cutoff_date"]).days


def filed_on_time(tax_period: str, filed_at: date | None) -> bool:
    past = days_past_cutoff(tax_period, filed_at)
    return past is not None and past <= 0
