"""Risk band rules. A judge will change the thresholds live, so they live in config —
these tests pin the behaviour, not the numbers."""

from datetime import date
from decimal import Decimal

from lockstep.models.enums import RiskBand
from lockstep.models.vendor_filing_history import VendorFilingHistory
from lockstep.services.vendor_scoring import predicted_late, risk_band, summarize_filing


def _history(tax_period: str, days_past: int | None, filed_day: int | None = None):
    return VendorFilingHistory(
        tax_period=tax_period,
        gstr1_filed=days_past is not None,
        gstr1_filed_at=date(2026, 9, filed_day) if filed_day else None,
        days_past_cutoff=days_past,
        invoice_count=3,
    )


def test_summarize_counts_on_time_periods():
    rows = [_history("042026", -3), _history("052026", -1), _history("062026", 4)]
    stats = summarize_filing(rows)

    assert stats.periods_observed == 3
    assert stats.periods_on_time == 2
    assert stats.on_time_rate == 2 / 3


def test_summarize_treats_the_thirteenth_itself_as_on_time():
    assert summarize_filing([_history("052026", 0)]).periods_on_time == 1


def test_summarize_ignores_unfiled_periods_in_the_average():
    stats = summarize_filing([_history("052026", 4), _history("062026", None)])
    assert stats.avg_days_past_cutoff == 4


def test_summarize_reports_the_typical_filing_day():
    """'They usually file around the 17th' — the sentence the whole pitch rests on."""
    rows = [_history("042026", 4, filed_day=17), _history("052026", 5, filed_day=18)]
    assert summarize_filing(rows).typical_filing_day == 18  # (17+18)/2 rounds to 18


def test_summarize_honours_the_history_window():
    rows = [_history(f"{m:02d}2026", -1) for m in range(1, 9)]
    assert summarize_filing(rows, window=6).periods_observed == 6


def test_summarize_with_no_history_is_unknown_not_zero():
    stats = summarize_filing([])
    assert stats.periods_observed == 0
    assert stats.on_time_rate is None


def test_band_low_for_a_reliable_filer():
    rows = [_history(f"{m:02d}2026", -2) for m in range(1, 7)]
    assert risk_band(summarize_filing(rows)) == RiskBand.LOW


def test_band_medium_between_thresholds():
    rows = [_history("012026", -2), _history("022026", -2), _history("032026", 5)]
    assert risk_band(summarize_filing(rows)) == RiskBand.MEDIUM


def test_band_high_for_a_chronic_late_filer():
    rows = [_history("012026", 5), _history("022026", 6), _history("032026", -1)]
    assert risk_band(summarize_filing(rows)) == RiskBand.HIGH


def test_band_high_when_overdue_with_exposure_regardless_of_history():
    """A perfect filer who hasn't filed yet, with money on it, is still today's problem."""
    rows = [_history(f"{m:02d}2026", -2) for m in range(1, 7)]
    stats = summarize_filing(rows)
    assert risk_band(stats, exposure=Decimal("184000"), filed_this_period=False) == RiskBand.HIGH


def test_band_unknown_without_history():
    assert risk_band(summarize_filing([])) == RiskBand.UNKNOWN


def test_predicted_late_only_when_unfiled_and_unreliable():
    unreliable = summarize_filing([_history("012026", 5), _history("022026", 6)])
    reliable = summarize_filing([_history("012026", -5), _history("022026", -6)])

    assert predicted_late(unreliable, filed_this_period=False) is True
    assert predicted_late(unreliable, filed_this_period=True) is False
    assert predicted_late(reliable, filed_this_period=False) is False
