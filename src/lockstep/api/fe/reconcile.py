"""POST /api/reconcile — accept parsed records from FE, run matching, persist, return full run."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.database import get_db
from lockstep.models.enums import RunStatus
from lockstep.models.invoice import Invoice
from lockstep.models.reconciliation_run import ReconciliationRun
from lockstep.schemas.fe_types import ReconcileRequest
from lockstep.services.fe_reconciler import reconcile

router = APIRouter(prefix="/reconcile", tags=["fe-reconcile"])


def _risk_category_to_match_status(cat: str) -> str:
    return {
        "matched": "EXACT_MATCH",
        "low_risk": "CLERICAL_MISMATCH",
        "high_risk": "MISSING_IN_GSTR2B",
        "cannot_file": "MISSING_IN_GSTR2B",
    }.get(cat, "PENDING")


def _risk_category_to_risk_tier(cat: str) -> str:
    return {
        "matched": "SAFE",
        "low_risk": "LOW_RISK",
        "high_risk": "HIGH_RISK",
        "cannot_file": "BLOCKED",
    }.get(cat, "PENDING")


@router.post("")
async def create_reconciliation(
    payload: ReconcileRequest,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    run_result = reconcile(
        payload.purchase_records,
        payload.gstr2b_records,
        payload.purchase_file_name,
        payload.gstr2b_file_name,
    )

    run_id = uuid.UUID(run_result.id)

    db_run = ReconciliationRun(
        id=run_id,
        name=f"{payload.purchase_file_name} vs {payload.gstr2b_file_name}",
        invoice_ledger_file_url="",
        gstr2b_file_url="",
        created_by=None,
        status=RunStatus.COMPLETED.value,
    )
    db.add(db_run)
    await db.flush()

    for record in run_result.records:
        invoice = Invoice(
            id=uuid.UUID(record.id),
            run_id=run_id,
            invoice_number=record.invoice_no,
            status=_risk_category_to_match_status(record.status),
            risk_tier=_risk_category_to_risk_tier(record.status),
            ai_summary=record.ai_summary,
            itc_amount=record.total_tax,
            raw_data={
                "invoice_no": record.invoice_no,
                "invoice_date": record.invoice_date,
                "supplier_name": record.supplier_name,
                "gstin": record.gstin,
                "taxable_value": record.taxable_value,
                "igst": record.igst,
                "cgst": record.cgst,
                "sgst": record.sgst,
            },
            action_status=record.action_status,
            activity_log=[e.model_dump(by_alias=True) for e in record.activity_log],
            match_confidence=int(record.match_confidence),
            ai_suggestions=[s.model_dump(by_alias=True) for s in record.ai_suggestions],
            purchase_data=record.purchase_record.model_dump(by_alias=True) if record.purchase_record else None,
            gstr2b_data=record.gstr2b_record.model_dump(by_alias=True) if record.gstr2b_record else None,
        )
        db.add(invoice)

    await db.commit()

    return JSONResponse(
        content=run_result.to_camel_dict(),
        status_code=status.HTTP_201_CREATED,
    )
