import os
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.api.deps import get_current_user
from lockstep.database import get_db
from lockstep.models.user import User
from lockstep.repositories import invoice_repository, run_repository
from lockstep.schemas.run import RiskTierCounts, RunCreateResponse, RunDetailOut, RunOut
from lockstep.storage import get_storage
from lockstep.tasks.reconciliation_tasks import run_reconciliation

router = APIRouter(prefix="/runs", tags=["runs"])


@router.post("", response_model=RunCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_run(
    name: str = Form(...),
    invoice_ledger_file: UploadFile = File(...),
    gstr2b_file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RunCreateResponse:
    for upload in (invoice_ledger_file, gstr2b_file):
        if not (upload.filename or "").lower().endswith(".csv"):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"{upload.filename} is not a .csv file"
            )

    run_id = uuid.uuid4()
    storage = get_storage()

    ledger_key = f"{run_id}/ledger_{os.path.basename(invoice_ledger_file.filename)}"
    gstr2b_key = f"{run_id}/gstr2b_{os.path.basename(gstr2b_file.filename)}"
    ledger_url = storage.save(ledger_key, await invoice_ledger_file.read())
    gstr2b_url = storage.save(gstr2b_key, await gstr2b_file.read())

    run = await run_repository.create(
        db,
        run_id=run_id,
        name=name,
        invoice_ledger_file_url=ledger_url,
        gstr2b_file_url=gstr2b_url,
        created_by=current_user.id,
    )
    await db.commit()

    run_reconciliation.delay(str(run_id))
    return RunCreateResponse(id=run.id, status=run.status)


@router.get("", response_model=list[RunOut])
async def list_runs(
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
) -> list[RunOut]:
    runs = await run_repository.list_runs(db)
    return [RunOut.model_validate(r) for r in runs]


@router.get("/{run_id}", response_model=RunDetailOut)
async def get_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RunDetailOut:
    run = await run_repository.get_by_id(db, run_id)
    if not run:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")

    counts = await invoice_repository.risk_tier_counts(db, run_id)
    return RunDetailOut(
        id=run.id,
        name=run.name,
        status=run.status,
        error_message=run.error_message,
        created_at=run.created_at,
        bucket_counts=RiskTierCounts.from_counts(counts),
    )
