import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.models.reconciliation_run import ReconciliationRun


async def create(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    name: str,
    invoice_ledger_file_url: str,
    gstr2b_file_url: str,
    created_by: uuid.UUID,
) -> ReconciliationRun:
    run = ReconciliationRun(
        id=run_id,
        name=name,
        invoice_ledger_file_url=invoice_ledger_file_url,
        gstr2b_file_url=gstr2b_file_url,
        created_by=created_by,
    )
    db.add(run)
    await db.flush()
    return run


async def get_by_id(db: AsyncSession, run_id: uuid.UUID) -> ReconciliationRun | None:
    result = await db.execute(select(ReconciliationRun).where(ReconciliationRun.id == run_id))
    return result.scalar_one_or_none()


async def list_runs(db: AsyncSession, limit: int = 50) -> list[ReconciliationRun]:
    result = await db.execute(
        select(ReconciliationRun).order_by(ReconciliationRun.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def mark_completed(db: AsyncSession, run: ReconciliationRun) -> None:
    run.status = "COMPLETED"
    await db.flush()


async def mark_failed(db: AsyncSession, run: ReconciliationRun, error_message: str) -> None:
    run.status = "FAILED"
    run.error_message = error_message[:2000]
    await db.flush()
