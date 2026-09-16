"""Vendor risk from filing *behaviour*, by rules. No model, no score to explain away.

A reconciliation tool can tell you a vendor has mismatches this month. Only filing
history lets you say "late in 3 of the last 4 periods, usually around the 17th" —
which is a prediction, and prediction is the product.

Thresholds come from config because a judge will ask to change them live.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.config import get_settings
from lockstep.models.enums import AT_RISK_STATUSES, RiskBand
from lockstep.models.invoice import Invoice
from lockstep.models.vendor import Vendor
from lockstep.models.vendor_filing_history import VendorFilingHistory


@dataclass
class FilingStats:
    periods_observed: int
    periods_on_time: int
    on_time_rate: float | None
    avg_days_past_cutoff: float | None
    typical_filing_day: int | None  # day of month they usually file


def summarize_filing(rows: list[VendorFilingHistory], window: int | None = None) -> FilingStats:
    """Pure: filing-history rows in, stats out. `rows` need not be sorted."""
    settings = get_settings()
    window = window or settings.risk_history_window
    recent = sorted(rows, key=lambda r: r.tax_period[2:] + r.tax_period[:2], reverse=True)[:window]

    if not recent:
        return FilingStats(0, 0, None, None, None)

    on_time = sum(1 for r in recent if r.days_past_cutoff is not None and r.days_past_cutoff <= 0)
    lateness = [r.days_past_cutoff for r in recent if r.days_past_cutoff is not None]
    filed_days = [r.gstr1_filed_at.day for r in recent if r.gstr1_filed_at is not None]

    return FilingStats(
        periods_observed=len(recent),
        periods_on_time=on_time,
        on_time_rate=on_time / len(recent),
        avg_days_past_cutoff=sum(lateness) / len(lateness) if lateness else None,
        typical_filing_day=round(sum(filed_days) / len(filed_days)) if filed_days else None,
    )


def risk_band(
    stats: FilingStats, exposure: Decimal = Decimal("0"), filed_this_period: bool = True
) -> RiskBand:
    """Low >= 0.9 on-time, Medium 0.6-0.9, High below 0.6 — or overdue with money on it."""
    settings = get_settings()
    if not filed_this_period and exposure > 0:
        return RiskBand.HIGH
    if stats.on_time_rate is None:
        return RiskBand.UNKNOWN
    if stats.on_time_rate >= settings.risk_on_time_rate_low:
        return RiskBand.LOW
    if stats.on_time_rate >= settings.risk_on_time_rate_medium:
        return RiskBand.MEDIUM
    return RiskBand.HIGH


def predicted_late(stats: FilingStats, filed_this_period: bool) -> bool:
    settings = get_settings()
    if filed_this_period or stats.on_time_rate is None:
        return False
    return stats.on_time_rate < settings.risk_on_time_rate_medium


@dataclass
class VendorRiskRow:
    vendor_id: str
    name: str
    gstin: str
    contact_email: str | None
    periods_observed: int
    on_time_rate: float | None
    avg_days_past_cutoff: float | None
    typical_filing_day: int | None
    filed_this_period: bool
    predicted_late: bool
    missing_invoice_count: int
    current_exposure: Decimal
    risk_band: str

    @property
    def priority(self) -> Decimal:
        """Exposure x risk — how the dashboard ranks vendors."""
        weight = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 2}[self.risk_band]
        return self.current_exposure * weight


async def get_vendor_risk(
    db: AsyncSession, period_id=None, tax_period: str | None = None, check_id=None
) -> list[VendorRiskRow]:
    """Vendor risk matrix. Exposure is scoped to the given period's latest check."""
    exposure_query = select(
        Invoice.vendor_id,
        func.count().label("missing_count"),
        func.coalesce(
            func.sum(Invoice.igst + Invoice.cgst + Invoice.sgst + Invoice.cess), 0
        ).label("exposure"),
    ).where(Invoice.status.in_(AT_RISK_STATUSES)).group_by(Invoice.vendor_id)
    if period_id is not None:
        if check_id is None:
            from lockstep.services.dashboard import latest_check_id

            check_id = await latest_check_id(db, period_id)
        exposure_query = exposure_query.where(
            Invoice.period_id == period_id, Invoice.check_id == check_id
        )

    exposure_by_vendor = {
        row.vendor_id: (row.missing_count, Decimal(row.exposure))
        for row in (await db.execute(exposure_query)).all()
        if row.vendor_id is not None
    }

    history_rows = (await db.execute(select(VendorFilingHistory))).scalars().all()
    history_by_vendor: dict[object, list[VendorFilingHistory]] = {}
    for row in history_rows:
        history_by_vendor.setdefault(row.vendor_id, []).append(row)

    vendors = (await db.execute(select(Vendor).where(Vendor.is_active))).scalars().all()

    results: list[VendorRiskRow] = []
    for vendor in vendors:
        history = history_by_vendor.get(vendor.id, [])
        stats = summarize_filing(history)
        missing_count, exposure = exposure_by_vendor.get(vendor.id, (0, Decimal("0")))
        filed = True
        if tax_period:
            current = next((h for h in history if h.tax_period == tax_period), None)
            filed = bool(current and current.gstr1_filed)
        band = risk_band(stats, exposure, filed)
        results.append(
            VendorRiskRow(
                vendor_id=str(vendor.id),
                name=vendor.name,
                gstin=vendor.gstin,
                contact_email=vendor.contact_email,
                periods_observed=stats.periods_observed,
                on_time_rate=stats.on_time_rate,
                avg_days_past_cutoff=stats.avg_days_past_cutoff,
                typical_filing_day=stats.typical_filing_day,
                filed_this_period=filed,
                predicted_late=predicted_late(stats, filed),
                missing_invoice_count=missing_count,
                current_exposure=exposure,
                risk_band=str(band),
            )
        )

    results.sort(key=lambda r: r.priority, reverse=True)
    return results
