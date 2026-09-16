import os
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.api.deps import get_current_user
from lockstep.core.exceptions import IngestionError
from lockstep.database import get_db
from lockstep.models.enums import CheckStatus
from lockstep.models.period import Client, ReconciliationCheck, ReconciliationPeriod
from lockstep.models.user import User
from lockstep.schemas.period import (
    CheckOut,
    ClientCreate,
    ClientOut,
    HeadlineOut,
    PeriodCreate,
    PeriodOut,
)
from lockstep.services import reconciliation
from lockstep.services.dashboard import check_delta, period_headline
from lockstep.services.periods import period_dates
from lockstep.storage import get_storage

router = APIRouter(tags=["periods"])

ACCEPTED_EXTENSIONS = (".csv", ".xlsx", ".xls")


async def _require_period(db: AsyncSession, period_id: uuid.UUID) -> ReconciliationPeriod:
    period = await db.get(ReconciliationPeriod, period_id)
    if period is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Period not found")
    return period


@router.get("/clients", response_model=list[ClientOut])
async def list_clients(
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
):
    clients = (await db.execute(select(Client).order_by(Client.legal_name))).scalars().all()
    return [ClientOut.model_validate(c) for c in clients]


@router.post("/clients", response_model=ClientOut, status_code=status.HTTP_201_CREATED)
async def create_client(
    body: ClientCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # ponytail: one org per deployment until real auth exists — multi-tenancy is in the
    # schema, not in the login, per the brief.
    client = Client(org_id=uuid.UUID(int=0), legal_name=body.legal_name, gstin=body.gstin)
    db.add(client)
    await db.commit()
    await db.refresh(client)
    return ClientOut.model_validate(client)


@router.get("/periods", response_model=list[PeriodOut])
async def list_periods(
    client_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # tax_period is MMYYYY, so a plain string sort orders by month — '082024' would
    # land between '092026' and '072026'. Sort year first, then month.
    query = select(ReconciliationPeriod).order_by(
        func.substr(ReconciliationPeriod.tax_period, 3, 4).desc(),
        func.substr(ReconciliationPeriod.tax_period, 1, 2).desc(),
    )
    if client_id:
        query = query.where(ReconciliationPeriod.client_id == client_id)
    periods = (await db.execute(query)).scalars().all()
    return [PeriodOut.model_validate(p) for p in periods]


@router.post("/periods", response_model=PeriodOut, status_code=status.HTTP_201_CREATED)
async def create_period(
    body: PeriodCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    existing = (
        await db.execute(
            select(ReconciliationPeriod).where(
                ReconciliationPeriod.client_id == body.client_id,
                ReconciliationPeriod.tax_period == body.tax_period,
            )
        )
    ).scalar_one_or_none()
    if existing:
        return PeriodOut.model_validate(existing)

    dates = period_dates(body.tax_period)
    period = ReconciliationPeriod(
        client_id=body.client_id,
        tax_period=body.tax_period,
        cutoff_date=dates["cutoff_date"],
        gstr2b_date=dates["gstr2b_date"],
        filing_due=dates["filing_due"],
    )
    db.add(period)
    await db.commit()
    await db.refresh(period)
    return PeriodOut.model_validate(period)


@router.get("/periods/{period_id}/headline", response_model=HeadlineOut)
async def get_headline(
    period_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """₹X at risk · N days to the 13th · M vendors haven't filed."""
    period = await _require_period(db, period_id)
    return HeadlineOut(**vars(await period_headline(db, period)))


@router.get("/periods/{period_id}/delta")
async def get_delta(
    period_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """What changed since the previous check — the reason a period holds many checks."""
    period = await _require_period(db, period_id)
    return await check_delta(db, period)


@router.get("/periods/{period_id}/checks", response_model=list[CheckOut])
async def list_checks(
    period_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _require_period(db, period_id)
    checks = (
        await db.execute(
            select(ReconciliationCheck)
            .where(ReconciliationCheck.period_id == period_id)
            .order_by(ReconciliationCheck.created_at.desc())
        )
    ).scalars().all()
    return [CheckOut.model_validate(c) for c in checks]


@router.post(
    "/periods/{period_id}/checks", response_model=CheckOut, status_code=status.HTTP_201_CREATED
)
async def create_check(
    period_id: uuid.UUID,
    ledger_file: UploadFile | None = File(None),
    gstr2b_file: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload ledger and/or 2B and reconcile. Re-run as often as you like before the 13th.

    Runs inline rather than via Celery: a check is seconds of work, and a synchronous
    result means the UI can show the new delta immediately.
    """
    period = await _require_period(db, period_id)
    if ledger_file is None and gstr2b_file is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Provide a ledger file, a 2B file, or both"
        )

    storage = get_storage()
    check = ReconciliationCheck(id=uuid.uuid4(), period_id=period.id, created_by=current_user.id)

    payloads: dict[str, tuple[str, bytes] | None] = {"ledger": None, "gstr2b": None}
    for label, upload in (("ledger", ledger_file), ("gstr2b", gstr2b_file)):
        if upload is None:
            continue
        filename = upload.filename or ""
        if not filename.lower().endswith(ACCEPTED_EXTENSIONS):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{filename} must be one of {', '.join(ACCEPTED_EXTENSIONS)}",
            )
        content = await upload.read()
        url = storage.save(f"{check.id}/{label}_{os.path.basename(filename)}", content)
        setattr(check, f"{label}_file_url", url)
        payloads[label] = (filename, content)

    db.add(check)
    await db.flush()

    try:
        await reconciliation.run_check(db, check, period, payloads["ledger"], payloads["gstr2b"])
    except IngestionError as exc:
        check.status = CheckStatus.FAILED.value
        check.error_message = str(exc)
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    await db.commit()
    await db.refresh(check)
    return CheckOut.model_validate(check)
