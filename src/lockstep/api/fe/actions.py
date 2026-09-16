"""PATCH action + POST nudge on individual records."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from lockstep.database import get_db
from lockstep.models.invoice import Invoice
from lockstep.schemas.fe_types import (
    GSTR2BRecordIn,
    ActionRequest,
    ActivityEntry,
    AiSuggestion,
    NudgeRequest,
    PurchaseRecordIn,
    ReconciledRecord,
)

router = APIRouter(prefix="/runs", tags=["fe-actions"])


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


async def _get_invoice(db: AsyncSession, run_id: uuid.UUID, record_id: uuid.UUID) -> Invoice:
    result = await db.execute(
        select(Invoice).where(Invoice.id == record_id, Invoice.run_id == run_id)
    )
    inv = result.scalar_one_or_none()
    if not inv:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Record not found")
    return inv


@router.patch("/{run_id}/records/{record_id}/action")
async def update_action(
    run_id: uuid.UUID,
    record_id: uuid.UUID,
    payload: ActionRequest,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    inv = await _get_invoice(db, run_id, record_id)

    inv.action_status = payload.action

    descriptions = {
        "flagged": "Record flagged for review — payment release paused pending verification",
        "escalated": "Record escalated to senior finance — requires immediate attention",
        "resolved": "Record marked as resolved — cleared for payment processing",
    }

    entry = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": payload.action,
        "description": descriptions[payload.action],
        "actor": "Finance Team",
    }

    activity_log = list(inv.activity_log or [])
    activity_log.append(entry)
    inv.activity_log = activity_log
    flag_modified(inv, "activity_log")

    await db.commit()
    await db.refresh(inv)

    return JSONResponse(content=_invoice_to_record(inv).to_camel_dict())


@router.post("/{run_id}/records/{record_id}/nudge")
async def send_nudge(
    run_id: uuid.UUID,
    record_id: uuid.UUID,
    payload: NudgeRequest,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    inv = await _get_invoice(db, run_id, record_id)

    supplier_name = inv.raw_data.get("supplier_name", "vendor")
    channel_label = "Email" if payload.channel == "email" else "WhatsApp"

    entry = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "nudge_sent",
        "description": f"Vendor nudge sent via {channel_label} to {supplier_name}",
        "actor": "Finance Team",
        "channel": payload.channel,
    }

    activity_log = list(inv.activity_log or [])
    activity_log.append(entry)
    inv.activity_log = activity_log
    flag_modified(inv, "activity_log")

    await db.commit()
    await db.refresh(inv)

    return JSONResponse(content=_invoice_to_record(inv).to_camel_dict())
