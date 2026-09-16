"""GET /api/runs and GET /api/runs/{run_id} — FE-shaped run data."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from lockstep.database import get_db
from lockstep.models.invoice import Invoice
from lockstep.models.reconciliation_run import ReconciliationRun
from lockstep.schemas.fe_types import (
    GSTR2BRecordIn,
    ActivityEntry,
    AiSuggestion,
    PurchaseRecordIn,
    ReconciledRecord,
    ReconciliationRunOut,
    ReconciliationRunSummary,
)

router = APIRouter(prefix="/runs", tags=["fe-runs"])


def _risk_tier_to_category(tier: str) -> str:
    return {
        "SAFE": "matched",
        "LOW_RISK": "low_risk",
        "HIGH_RISK": "high_risk",
        "BLOCKED": "cannot_file",
        "PENDING": "high_risk",
    }.get(str(tier), "high_risk")


def _invoice_to_record(inv: Invoice) -> ReconciledRecord:
    cat = _risk_tier_to_category(str(inv.risk_tier))
    return ReconciledRecord(
        id=str(inv.id),
        invoice_no=inv.invoice_number,
        invoice_date=inv.raw_data.get("invoice_date", ""),
        supplier_name=inv.raw_data.get("supplier_name", ""),
        gstin=inv.raw_data.get("gstin", ""),
        taxable_value=float(inv.raw_data.get("taxable_value", 0)),
        igst=float(inv.raw_data.get("igst", 0)),
        cgst=float(inv.raw_data.get("cgst", 0)),
        sgst=float(inv.raw_data.get("sgst", 0)),
        total_tax=float(inv.itc_amount or 0),
        status=cat,
        match_confidence=inv.match_confidence or 0,
        ai_summary=inv.ai_summary or "",
        ai_suggestions=[AiSuggestion(**s) for s in (inv.ai_suggestions or [])],
        action_status=inv.action_status or "none",
        activity_log=[ActivityEntry(**e) for e in (inv.activity_log or [])],
        purchase_record=PurchaseRecordIn(**inv.purchase_data) if inv.purchase_data else None,
        gstr2b_record=GSTR2BRecordIn(**inv.gstr2b_data) if inv.gstr2b_data else None,
    )


def _build_summary(run: ReconciliationRun, records: list[ReconciledRecord]) -> dict:
    matched = sum(1 for r in records if r.status == "matched")
    low_risk = sum(1 for r in records if r.status == "low_risk")
    high_risk = sum(1 for r in records if r.status == "high_risk")
    cannot_file = sum(1 for r in records if r.status == "cannot_file")
    total_taxable = sum(r.taxable_value for r in records)
    total_tax_at_risk = sum(
        r.total_tax for r in records if r.status in ("high_risk", "cannot_file")
    )
    purchase_file = run.name.split(" vs ")[0] if " vs " in run.name else run.name
    gstr2b_file = run.name.split(" vs ")[1] if " vs " in run.name else ""

    return {
        "id": str(run.id),
        "created_at": run.created_at.isoformat() if run.created_at else "",
        "purchase_file_name": purchase_file,
        "gstr2b_file_name": gstr2b_file,
        "total_records": len(records),
        "matched_count": matched,
        "low_risk_count": low_risk,
        "high_risk_count": high_risk,
        "cannot_file_count": cannot_file,
        "total_taxable_value": total_taxable,
        "total_tax_at_risk": total_tax_at_risk,
    }


@router.get("")
async def list_runs(db: AsyncSession = Depends(get_db)) -> JSONResponse:
    result = await db.execute(
        select(ReconciliationRun)
        .options(selectinload(ReconciliationRun.invoices))
        .order_by(ReconciliationRun.created_at.desc())
    )
    runs = result.scalars().all()

    summaries = []
    for run in runs:
        records = [_invoice_to_record(inv) for inv in run.invoices]
        data = _build_summary(run, records)
        summaries.append(ReconciliationRunSummary(**data).to_camel_dict())

    return JSONResponse(content=summaries)


@router.get("/{run_id}")
async def get_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    result = await db.execute(
        select(ReconciliationRun)
        .options(selectinload(ReconciliationRun.invoices))
        .where(ReconciliationRun.id == run_id)
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")

    records = [_invoice_to_record(inv) for inv in run.invoices]
    summary = _build_summary(run, records)

    run_out = ReconciliationRunOut(**summary, records=records)
    return JSONResponse(content=run_out.to_camel_dict())
