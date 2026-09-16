from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.models.vendor import Vendor


async def get_by_gstin(db: AsyncSession, gstin: str) -> Vendor | None:
    result = await db.execute(select(Vendor).where(Vendor.gstin == gstin))
    return result.scalar_one_or_none()


async def get_or_create(db: AsyncSession, *, gstin: str, name: str) -> Vendor:
    vendor = await get_by_gstin(db, gstin)
    if vendor:
        return vendor
    vendor = Vendor(gstin=gstin, name=name or gstin)
    db.add(vendor)
    await db.flush()
    return vendor
