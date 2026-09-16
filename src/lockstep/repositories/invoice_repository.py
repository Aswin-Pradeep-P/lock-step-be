import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.models.enums import InvoiceRiskTier
from lockstep.models.invoice import Invoice


async def bulk_create(db: AsyncSession, invoices: list[Invoice]) -> None:
    db.add_all(invoices)
    await db.flush()


async def list_for_run(
    db: AsyncSession,
    run_id: uuid.UUID,
    risk_tier: InvoiceRiskTier | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[Invoice]:
    query = select(Invoice).where(Invoice.run_id == run_id)
    if risk_tier is not None:
        query = query.where(Invoice.risk_tier == risk_tier)
    query = query.order_by(Invoice.invoice_number).limit(limit).offset(offset)
    result = await db.execute(query)
    return list(result.scalars().all())


async def risk_tier_counts(db: AsyncSession, run_id: uuid.UUID) -> dict[InvoiceRiskTier, int]:
    query = (
        select(Invoice.risk_tier, func.count())
        .where(Invoice.run_id == run_id)
        .group_by(Invoice.risk_tier)
    )
    result = await db.execute(query)
    return dict(result.all())


async def get_vendor_names(db: AsyncSession, vendor_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    from lockstep.models.vendor import Vendor

    if not vendor_ids:
        return {}
    result = await db.execute(select(Vendor.id, Vendor.name).where(Vendor.id.in_(vendor_ids)))
    return dict(result.all())
