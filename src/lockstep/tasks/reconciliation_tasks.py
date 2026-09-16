"""The reconciliation pipeline, run as a Celery task off the request path.

Parse both CSVs -> deterministic 3-pass match -> deterministic Sec 16(4)
deadline math -> AI classification for everything except exact matches ->
persist. Any failure marks the run FAILED with the error message rather than
leaving it stuck in PROCESSING.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.celery_app import celery_app
from lockstep.config import get_settings
from lockstep.database import AsyncSessionLocal, engine
from lockstep.models.enums import InvoiceMatchStatus, InvoiceRiskTier
from lockstep.models.invoice import Invoice
from lockstep.models.reconciliation_run import ReconciliationRun
from lockstep.repositories import invoice_repository, run_repository, vendor_repository
from lockstep.services import ai_classifier, matching, risk_rules
from lockstep.services.ingestion import parse_csv, to_canonical_rows
from lockstep.storage import get_storage

settings = get_settings()
logger = logging.getLogger(__name__)


@celery_app.task(name="lockstep.run_reconciliation")
def run_reconciliation(run_id: str) -> None:
    asyncio.run(_run_reconciliation_async(uuid.UUID(run_id)))


async def _run_reconciliation_async(run_id: uuid.UUID) -> None:
    # Celery calls asyncio.run() per task, each spinning up a fresh event
    # loop; the engine's connection pool must not hand out a connection bound
    # to a previous (now-closed) loop, so drop the pool before using it here.
    await engine.dispose()

    # Everything below — including the initial fetch — is inside this try, so
    # a run can never end up stuck in PROCESSING forever with no error to show:
    # any failure at any stage falls through to the mark_failed write below.
    try:
        async with AsyncSessionLocal() as db:
            run = await run_repository.get_by_id(db, run_id)
            if run is None:
                logger.error("Reconciliation run %s not found", run_id)
                return

            await _process_run(db, run)
            await run_repository.mark_completed(db, run)
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - any failure here must fail the run, not crash the worker
        logger.exception("Reconciliation run %s failed", run_id)
        async with AsyncSessionLocal() as fail_db:
            failed_run = await run_repository.get_by_id(fail_db, run_id)
            if failed_run:
                await run_repository.mark_failed(fail_db, failed_run, str(exc))
                await fail_db.commit()


async def _process_run(db: AsyncSession, run: ReconciliationRun) -> None:
    storage = get_storage()
    ledger_rows_raw, ledger_columns = parse_csv(storage.read(run.invoice_ledger_file_url))
    gstr2b_rows_raw, gstr2b_columns = parse_csv(storage.read(run.gstr2b_file_url))

    ledger_rows = to_canonical_rows(ledger_rows_raw, ledger_columns)
    gstr2b_rows = to_canonical_rows(gstr2b_rows_raw, gstr2b_columns)

    match_results = matching.match_invoices(ledger_rows, gstr2b_rows)
    if not match_results:
        raise ValueError("No invoices were produced from the uploaded CSVs")

    today = date.today()
    invoice_models: list[Invoice] = []
    pending_ai: list[tuple[Invoice, dict]] = []

    for result in match_results:
        canonical = result.ledger_row or result.gstr2b_row
        vendor = None
        if canonical.gstin:
            vendor = await vendor_repository.get_or_create(
                db, gstin=canonical.gstin, name=canonical.vendor_name
            )

        invoice = Invoice(
            run_id=run.id,
            vendor_id=vendor.id if vendor else None,
            invoice_number=canonical.invoice_number,
            status=result.status,
            raw_data=canonical.raw,
            itc_amount=canonical.itc_amount,
        )

        if result.status == InvoiceMatchStatus.EXACT_MATCH:
            invoice.risk_tier = InvoiceRiskTier.SAFE
            invoice.ai_summary = ai_classifier.SAFE_SUMMARY_MARKDOWN
        else:
            deadline, days_remaining = None, None
            if canonical.invoice_date:
                deadline = risk_rules.compute_recoverable_until(canonical.invoice_date)
                days_remaining = risk_rules.days_remaining(deadline, today)
                invoice.recoverable_until = deadline

            vendor_name = canonical.vendor_name or (vendor.name if vendor else "Unknown vendor")
            pending_ai.append((
                invoice,
                {
                    "invoice_number": canonical.invoice_number,
                    "vendor_name": vendor_name,
                    "gstin": canonical.gstin,
                    "taxable_value": canonical.taxable_value,
                    "description": canonical.description,
                    "match_status": result.status.value,
                    "recoverable_until": deadline.isoformat() if deadline else None,
                    "days_remaining": days_remaining,
                    "sec17_5_hint": risk_rules.sec17_5_hint(canonical.description),
                },
            ))

        invoice_models.append(invoice)

    await invoice_repository.bulk_create(db, invoice_models)
    await _classify_pending(pending_ai)


async def _classify_pending(pending: list[tuple[Invoice, dict]]) -> None:
    semaphore = asyncio.Semaphore(settings.ai_concurrency)

    async def classify_one(invoice: Invoice, context: dict) -> None:
        async with semaphore:
            assessment = await ai_classifier.classify_with_retry_and_fallback(context)
        invoice.risk_tier = ai_classifier.RISK_TIER_MAP[assessment.risk_tier]
        invoice.ai_summary = assessment.markdown_summary
        invoice.citations = assessment.citations
        if not assessment.recoverable:
            invoice.recoverable_until = None

    await asyncio.gather(*(classify_one(inv, ctx) for inv, ctx in pending))
