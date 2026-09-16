import csv
import io
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.api.deps import get_current_user
from lockstep.database import get_db
from lockstep.models.enums import InvoiceRiskTier
from lockstep.models.user import User
from lockstep.repositories import invoice_repository, run_repository
from lockstep.schemas.invoice import ACTION_BY_RISK_TIER, InvoiceOut

router = APIRouter(prefix="/runs/{run_id}/invoices", tags=["invoices"])


def _parse_risk_tier(risk_tier: str | None) -> InvoiceRiskTier | None:
    if risk_tier is None:
        return None
    try:
        return InvoiceRiskTier(risk_tier.upper())
    except ValueError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Unknown risk_tier: {risk_tier}"
        ) from None


async def _require_run(db: AsyncSession, run_id: uuid.UUID):
    run = await run_repository.get_by_id(db, run_id)
    if not run:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return run


@router.get("", response_model=list[InvoiceOut])
async def list_invoices(
    run_id: uuid.UUID,
    risk_tier: str | None = Query(None),
    limit: int = Query(200, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[InvoiceOut]:
    await _require_run(db, run_id)
    tier = _parse_risk_tier(risk_tier)
    invoices = await invoice_repository.list_for_run(db, run_id, tier, limit, offset)
    vendor_names = await invoice_repository.get_vendor_names(
        db, [i.vendor_id for i in invoices if i.vendor_id]
    )
    return [InvoiceOut.build(inv, vendor_names.get(inv.vendor_id)) for inv in invoices]


@router.get("/export")
async def export_invoices(
    run_id: uuid.UUID,
    risk_tier: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    await _require_run(db, run_id)
    tier = _parse_risk_tier(risk_tier)
    invoices = await invoice_repository.list_for_run(db, run_id, tier, limit=100_000, offset=0)

    raw_columns: list[str] = []
    for inv in invoices:
        for key in inv.raw_data:
            if key not in raw_columns:
                raw_columns.append(key)

    fieldnames = [*raw_columns, "status", "risk_tier", "action", "ai_summary"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for inv in invoices:
        risk_tier_value = str(inv.risk_tier)
        row = {
            **{col: inv.raw_data.get(col, "") for col in raw_columns},
            "status": str(inv.status),
            "risk_tier": risk_tier_value,
            "action": ACTION_BY_RISK_TIER.get(risk_tier_value, "Review"),
            "ai_summary": (inv.ai_summary or "").replace("\n", " "),
        }
        writer.writerow(row)

    buffer.seek(0)
    filename = f"run_{run_id}_invoices.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
