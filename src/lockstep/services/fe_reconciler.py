"""Reconciliation engine ported from the frontend's server/lib/reconciler.ts.

Three-pass matching (exact -> fuzzy -> missing), AI suggestion generation,
and summary text — all deterministic, no LLM call required.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from lockstep.schemas.fe_types import (
    GSTR2BRecordIn,
    ActivityEntry,
    AiSuggestion,
    PurchaseRecordIn,
    ReconciledRecord,
    ReconciliationRunOut,
    RiskCategory,
)

TAX_EXACT_TOLERANCE = 1.0
TAX_FUZZY_TOLERANCE_PERCENT = 0.05
INVOICE_FUZZY_MAX_DISTANCE = 2
SUPPLIER_FUZZY_MAX_DISTANCE = 3


def _levenshtein(a: str, b: str) -> int:
    m, n = len(a), len(b)
    if m == 0:
        return n
    if n == 0:
        return m
    matrix = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        matrix[i][0] = i
    for j in range(n + 1):
        matrix[0][j] = j
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            matrix[i][j] = min(
                matrix[i - 1][j] + 1,
                matrix[i][j - 1] + 1,
                matrix[i - 1][j - 1] + cost,
            )
    return matrix[m][n]


def _normalize(value: str) -> str:
    return value.strip().lower()


def _purchase_total_tax(r: PurchaseRecordIn) -> float:
    return (r.igst_input or 0) + (r.cgst_input or 0) + (r.sgst_input or 0)


def _gstr2b_total_tax(r: GSTR2BRecordIn) -> float:
    return r.igst + r.cgst + r.sgst


def _is_itc_unavailable(r: GSTR2BRecordIn) -> bool:
    return _normalize(r.itc_availability) == "no"


def _tax_within_tolerance(purchase_tax: float, gstr2b_tax: float, tolerance: float) -> bool:
    return abs(purchase_tax - gstr2b_tax) <= tolerance


def _tax_within_percent(purchase_tax: float, gstr2b_tax: float, percent: float) -> bool:
    if purchase_tax == 0 and gstr2b_tax == 0:
        return True
    baseline = max(purchase_tax, gstr2b_tax, 1)
    return abs(purchase_tax - gstr2b_tax) / baseline <= percent


def _supplier_similarity(purchase_supplier: str, gstr2b_trade_name: str) -> float:
    a = _normalize(purchase_supplier)
    b = _normalize(gstr2b_trade_name)
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9
    distance = _levenshtein(a, b)
    max_len = max(len(a), len(b), 1)
    return max(0.0, 1.0 - distance / max_len)


def _invoice_similarity(a_raw: str, b_raw: str) -> float:
    a = _normalize(a_raw)
    b = _normalize(b_raw)
    if a == b:
        return 1.0
    distance = _levenshtein(a, b)
    max_len = max(len(a), len(b), 1)
    return max(0.0, 1.0 - distance / max_len)


def _find_vendor_itc_status(
    purchase: PurchaseRecordIn, gstr2b_records: list[GSTR2BRecordIn]
) -> str:
    for g in gstr2b_records:
        if _supplier_similarity(purchase.supplier, g.trade_name) >= 0.7:
            return "no" if _is_itc_unavailable(g) else "yes"
    return "unknown"


def _format_inr(value: float) -> str:
    return f"₹{value:,.0f}"


# ── Suggestion Generation ────────────────────────────────────────────────────

def _generate_suggestions(
    purchase: PurchaseRecordIn,
    gstr2b: GSTR2BRecordIn | None,
    status: RiskCategory,
) -> list[AiSuggestion]:
    suggestions: list[AiSuggestion] = []
    total_tax = _purchase_total_tax(purchase)

    if status == "matched":
        suggestions.append(AiSuggestion(
            id=str(uuid.uuid4()),
            action="accept_risk",
            label="Claim ITC",
            description=f"Invoice verified — safe to include {_format_inr(total_tax)} ITC in your next GSTR-3B filing.",
            confidence=98,
        ))
        return suggestions

    if status == "low_risk" and gstr2b:
        inv_distance = _levenshtein(
            _normalize(purchase.voucher_ref_no),
            _normalize(gstr2b.invoice_no),
        )
        if inv_distance > 0 and inv_distance <= INVOICE_FUZZY_MAX_DISTANCE:
            suggestions.append(AiSuggestion(
                id=str(uuid.uuid4()),
                action="auto_correct",
                label="Auto-correct invoice number",
                description=(
                    f'Likely typo: your records show "{purchase.voucher_ref_no}" but GSTR-2B shows '
                    f'"{gstr2b.invoice_no}" ({inv_distance} character difference). Auto-correct to match and claim ITC.'
                ),
                confidence=85,
            ))
        tax_diff = abs(total_tax - _gstr2b_total_tax(gstr2b))
        if tax_diff > TAX_EXACT_TOLERANCE:
            suggestions.append(AiSuggestion(
                id=str(uuid.uuid4()),
                action="nudge_vendor",
                label="Request vendor to amend",
                description=(
                    f"Tax mismatch of {_format_inr(tax_diff)} — ask {gstr2b.trade_name} "
                    f"to verify and amend their GSTR-1 filing."
                ),
                confidence=75,
            ))

    if status == "high_risk":
        suggestions.append(AiSuggestion(
            id=str(uuid.uuid4()),
            action="nudge_vendor",
            label="Nudge vendor to file GSTR-1",
            description=(
                f"{purchase.supplier} has not reported this invoice. Send a compliance "
                f"reminder — {_format_inr(total_tax)} ITC is blocked until they file."
            ),
            confidence=90,
        ))
        suggestions.append(AiSuggestion(
            id=str(uuid.uuid4()),
            action="escalate_urgent",
            label="Escalate — ITC deadline approaching",
            description=(
                f"If unresolved within 180 days, you permanently lose {_format_inr(total_tax)} in ITC. "
                f"Flag for senior finance review now."
            ),
            confidence=80,
        ))
        suggestions.append(AiSuggestion(
            id=str(uuid.uuid4()),
            action="switch_vendor",
            label="Consider alternate vendor",
            description=(
                f"{purchase.supplier} has compliance issues. Evaluate switching to a vendor "
                f"with higher GST filing compliance to avoid recurring ITC risk."
            ),
            confidence=60,
        ))

    if status == "cannot_file":
        reason = (gstr2b.reason if gstr2b else "") or "ITC marked unavailable"
        suggestions.append(AiSuggestion(
            id=str(uuid.uuid4()),
            action="escalate_urgent",
            label="Do not claim — escalate to tax advisor",
            description=(
                f"ITC of {_format_inr(total_tax)} is legally blocked: {reason}. "
                f"Consult your tax advisor before including in GSTR-3B."
            ),
            confidence=95,
        ))
        if gstr2b and gstr2b.reason and "payment not made" in gstr2b.reason:
            suggestions.append(AiSuggestion(
                id=str(uuid.uuid4()),
                action="nudge_vendor",
                label="Clear pending payment to unblock ITC",
                description=(
                    "Rule 37 default — ITC is blocked because payment was not made within 180 days. "
                    "Clear the outstanding amount to restore eligibility."
                ),
                confidence=88,
            ))
        suggestions.append(AiSuggestion(
            id=str(uuid.uuid4()),
            action="switch_vendor",
            label="Flag vendor for review",
            description=(
                f"{purchase.supplier} has ITC-blocked invoices. Review vendor compliance score "
                f"and consider alternate suppliers for future orders."
            ),
            confidence=55,
        ))

    return suggestions


# ── Summary Text ─────────────────────────────────────────────────────────────

def _summarize_exact_match(purchase: PurchaseRecordIn, gstr2b: GSTR2BRecordIn) -> str:
    if _is_itc_unavailable(gstr2b):
        return (
            f"Invoice {purchase.voucher_ref_no} from {purchase.supplier} appears in GSTR-2B "
            f"but ITC is marked unavailable ({gstr2b.reason or 'no reason provided'}). "
            f"Cannot claim input tax credit for this invoice."
        )
    return (
        f"Invoice {purchase.voucher_ref_no} exactly matches GSTR-2B entry from "
        f"{gstr2b.trade_name} ({gstr2b.gstin}). Tax amounts align within "
        f"₹{TAX_EXACT_TOLERANCE:.0f}. Safe to claim ITC."
    )


def _summarize_fuzzy_match(
    purchase: PurchaseRecordIn, gstr2b: GSTR2BRecordIn, inv_distance: int
) -> str:
    if inv_distance > 0:
        return (
            f'Likely match with minor discrepancy: purchase register shows '
            f'"{purchase.voucher_ref_no}" while GSTR-2B shows "{gstr2b.invoice_no}" '
            f'(edit distance {inv_distance}). Supplier {purchase.supplier} matches '
            f'{gstr2b.trade_name}. Review before filing.'
        )
    purchase_tax = _purchase_total_tax(purchase)
    gstr2b_tax = _gstr2b_total_tax(gstr2b)
    diff = abs(purchase_tax - gstr2b_tax)
    return (
        f"Supplier and invoice align loosely for {purchase.voucher_ref_no}, but tax differs "
        f"by ₹{diff:.2f} (purchase: ₹{purchase_tax:.2f}, GSTR-2B: ₹{gstr2b_tax:.2f}). "
        f"Verify with vendor before claiming ITC."
    )


def _summarize_missing(
    purchase: PurchaseRecordIn, status: RiskCategory, vendor_itc: str
) -> str:
    if status == "cannot_file":
        return (
            f"Invoice {purchase.voucher_ref_no} from {purchase.supplier} is not eligible for ITC. "
            f"Vendor records indicate ITC availability is blocked. Do not include in GSTR-3B filing."
        )
    if vendor_itc == "unknown":
        return (
            f"Invoice {purchase.voucher_ref_no} from {purchase.supplier} "
            f"(₹{purchase.gross_total:,.0f}) has no corresponding GSTR-2B entry. "
            f"Vendor may not have filed returns. ITC claim is at high risk."
        )
    return (
        f"Invoice {purchase.voucher_ref_no} from {purchase.supplier} is missing from GSTR-2B "
        f"despite vendor filing other invoices. Follow up with vendor to reconcile before "
        f"claiming ₹{_purchase_total_tax(purchase):,.0f} in ITC."
    )


# ── Build a single record ────────────────────────────────────────────────────

def _build_reconciled_record(
    purchase: PurchaseRecordIn,
    gstr2b: GSTR2BRecordIn | None,
    status: RiskCategory,
    confidence: int,
    ai_summary: str,
) -> ReconciledRecord:
    igst = gstr2b.igst if gstr2b else (purchase.igst_input or 0)
    cgst = gstr2b.cgst if gstr2b else (purchase.cgst_input or 0)
    sgst = gstr2b.sgst if gstr2b else (purchase.sgst_input or 0)
    taxable_value = (
        gstr2b.taxable_value if gstr2b else purchase.gross_total - _purchase_total_tax(purchase)
    )

    record_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    created_entry = ActivityEntry(
        id=str(uuid.uuid4()),
        timestamp=now,
        type="created",
        description="Record created during reconciliation",
        actor="System",
    )

    return ReconciledRecord(
        id=record_id,
        invoice_no=purchase.voucher_ref_no,
        invoice_date=purchase.voucher_ref_date or purchase.date,
        supplier_name=purchase.supplier,
        gstin=gstr2b.gstin if gstr2b else "",
        taxable_value=taxable_value,
        igst=igst,
        cgst=cgst,
        sgst=sgst,
        total_tax=igst + cgst + sgst,
        status=status,
        match_confidence=confidence,
        ai_summary=ai_summary,
        ai_suggestions=_generate_suggestions(purchase, gstr2b, status),
        action_status="none",
        activity_log=[created_entry],
        purchase_record=purchase,
        gstr2b_record=gstr2b,
    )


# ── Main reconcile function ──────────────────────────────────────────────────

def reconcile(
    purchase_records: list[PurchaseRecordIn],
    gstr2b_records: list[GSTR2BRecordIn],
    purchase_file_name: str,
    gstr2b_file_name: str,
) -> ReconciliationRunOut:
    records: list[ReconciledRecord] = []
    unmatched_gstr2b = list(range(len(gstr2b_records)))
    unmatched_purchases: list[int] = []

    # Pass 1: Exact match on invoice number + tax within ₹1
    for p_idx, purchase in enumerate(purchase_records):
        purchase_invoice = _normalize(purchase.voucher_ref_no)
        if not purchase_invoice:
            unmatched_purchases.append(p_idx)
            continue
        purchase_tax = _purchase_total_tax(purchase)

        exact_idx = None
        for g_pos, g_idx in enumerate(unmatched_gstr2b):
            gstr2b = gstr2b_records[g_idx]
            gstr2b_invoice = _normalize(gstr2b.invoice_no)
            if not gstr2b_invoice:
                continue
            if gstr2b_invoice == purchase_invoice and _tax_within_tolerance(
                purchase_tax, _gstr2b_total_tax(gstr2b), TAX_EXACT_TOLERANCE
            ):
                exact_idx = g_pos
                break

        if exact_idx is None:
            unmatched_purchases.append(p_idx)
            continue

        g_idx = unmatched_gstr2b.pop(exact_idx)
        gstr2b = gstr2b_records[g_idx]
        status: RiskCategory = "cannot_file" if _is_itc_unavailable(gstr2b) else "matched"
        confidence = 95 if _is_itc_unavailable(gstr2b) else 100

        records.append(
            _build_reconciled_record(
                purchase, gstr2b, status, confidence,
                _summarize_exact_match(purchase, gstr2b),
            )
        )

    # Pass 2: Fuzzy match on remaining
    still_unmatched: list[int] = []
    for p_idx in unmatched_purchases:
        purchase = purchase_records[p_idx]
        purchase_invoice_norm = _normalize(purchase.voucher_ref_no)
        if not purchase_invoice_norm:
            still_unmatched.append(p_idx)
            continue
        purchase_tax = _purchase_total_tax(purchase)
        best_pos = -1
        best_score = 0.0
        best_inv_distance = INVOICE_FUZZY_MAX_DISTANCE + 1

        for g_pos, g_idx in enumerate(unmatched_gstr2b):
            gstr2b = gstr2b_records[g_idx]
            gstr2b_invoice_norm = _normalize(gstr2b.invoice_no)
            if not gstr2b_invoice_norm:
                continue
            inv_distance = _levenshtein(purchase_invoice_norm, gstr2b_invoice_norm)
            if inv_distance > INVOICE_FUZZY_MAX_DISTANCE:
                continue
            supplier_score = _supplier_similarity(purchase.supplier, gstr2b.trade_name)
            if supplier_score < 0.5:
                continue
            if not _tax_within_percent(
                purchase_tax, _gstr2b_total_tax(gstr2b), TAX_FUZZY_TOLERANCE_PERCENT
            ):
                continue

            inv_score = _invoice_similarity(purchase.voucher_ref_no, gstr2b.invoice_no)
            tax_diff_score = 1.0 - abs(purchase_tax - _gstr2b_total_tax(gstr2b)) / max(
                purchase_tax, _gstr2b_total_tax(gstr2b), 1
            )
            score = inv_score * 0.5 + supplier_score * 0.3 + tax_diff_score * 0.2

            if score > best_score:
                best_score = score
                best_pos = g_pos
                best_inv_distance = inv_distance

        if best_pos == -1:
            still_unmatched.append(p_idx)
            continue

        g_idx = unmatched_gstr2b.pop(best_pos)
        gstr2b = gstr2b_records[g_idx]
        confidence = min(90, max(60, round(60 + best_score * 30)))

        fuzzy_status: RiskCategory = "cannot_file" if _is_itc_unavailable(gstr2b) else "low_risk"
        if _is_itc_unavailable(gstr2b):
            fuzzy_summary = (
                f'Invoice {purchase.voucher_ref_no} from {purchase.supplier} fuzzy-matches '
                f'GSTR-2B entry "{gstr2b.invoice_no}" but ITC is marked unavailable '
                f'({gstr2b.reason or "no reason provided"}). Cannot claim input tax credit.'
            )
        else:
            fuzzy_summary = _summarize_fuzzy_match(purchase, gstr2b, best_inv_distance)

        records.append(
            _build_reconciled_record(purchase, gstr2b, fuzzy_status, confidence, fuzzy_summary)
        )

    # Pass 3: Missing — unmatched purchase records
    for p_idx in still_unmatched:
        purchase = purchase_records[p_idx]
        vendor_itc = _find_vendor_itc_status(purchase, gstr2b_records)
        status = "cannot_file" if vendor_itc == "no" else "high_risk"
        confidence = 85 if vendor_itc == "no" else 70

        records.append(
            _build_reconciled_record(
                purchase, None, status, confidence,
                _summarize_missing(purchase, status, vendor_itc),
            )
        )

    # Compute stats
    matched_count = sum(1 for r in records if r.status == "matched")
    low_risk_count = sum(1 for r in records if r.status == "low_risk")
    high_risk_count = sum(1 for r in records if r.status == "high_risk")
    cannot_file_count = sum(1 for r in records if r.status == "cannot_file")
    total_taxable_value = sum(r.taxable_value for r in records)
    total_tax_at_risk = sum(
        r.total_tax for r in records if r.status in ("high_risk", "cannot_file")
    )

    return ReconciliationRunOut(
        id=str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc).isoformat(),
        purchase_file_name=purchase_file_name,
        gstr2b_file_name=gstr2b_file_name,
        total_records=len(records),
        matched_count=matched_count,
        low_risk_count=low_risk_count,
        high_risk_count=high_risk_count,
        cannot_file_count=cannot_file_count,
        total_taxable_value=total_taxable_value,
        total_tax_at_risk=total_tax_at_risk,
        records=records,
    )
