"""Deterministic 3-pass reconciliation: exact -> clerical/fuzzy -> missing.

Kept entirely rule-based (no LLM) because matching outcomes are the numbers
a finance team's ITC total depends on — they need to be reproducible and
explainable without an API call.
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz

from lockstep.models.enums import InvoiceMatchStatus
from lockstep.services.ingestion import CanonicalRow

FUZZY_INVOICE_NUMBER_THRESHOLD = 85.0
FUZZY_GSTIN_THRESHOLD = 90.0
AMOUNT_TOLERANCE_RATIO = 0.01  # 1%
AMOUNT_TOLERANCE_ABSOLUTE = 1.0  # ₹1, for near-zero amounts


@dataclass
class MatchResult:
    ledger_row: CanonicalRow | None
    gstr2b_row: CanonicalRow | None
    status: InvoiceMatchStatus


def _amounts_close(a: float, b: float) -> bool:
    tolerance = max(AMOUNT_TOLERANCE_ABSOLUTE, AMOUNT_TOLERANCE_RATIO * max(abs(a), abs(b)))
    return abs(a - b) <= tolerance


def match_invoices(
    ledger_rows: list[CanonicalRow], gstr2b_rows: list[CanonicalRow]
) -> list[MatchResult]:
    results: list[MatchResult] = []
    remaining_gstr2b = list(range(len(gstr2b_rows)))

    # Pass 1: exact match on (normalized GSTIN, normalized invoice number),
    # amount within tolerance.
    exact_index: dict[tuple[str, str], list[int]] = {}
    for idx in remaining_gstr2b:
        row = gstr2b_rows[idx]
        key = (row.gstin_normalized, row.invoice_number_normalized)
        exact_index.setdefault(key, []).append(idx)

    unmatched_ledger: list[int] = []
    for l_idx, ledger_row in enumerate(ledger_rows):
        key = (ledger_row.gstin_normalized, ledger_row.invoice_number_normalized)
        candidates = exact_index.get(key, [])
        matched_idx = next(
            (c for c in candidates if c in remaining_gstr2b and _amounts_close(
                ledger_row.taxable_value, gstr2b_rows[c].taxable_value
            )),
            None,
        )
        if matched_idx is not None:
            remaining_gstr2b.remove(matched_idx)
            results.append(
                MatchResult(ledger_row, gstr2b_rows[matched_idx], InvoiceMatchStatus.EXACT_MATCH)
            )
        else:
            unmatched_ledger.append(l_idx)

    # Pass 2: fuzzy/clerical match among what's left — same-ish GSTIN and
    # invoice number (typo-tolerant), amount within tolerance.
    still_unmatched_ledger: list[int] = []
    for l_idx in unmatched_ledger:
        ledger_row = ledger_rows[l_idx]
        best_idx, best_score = None, 0.0
        for g_idx in remaining_gstr2b:
            gstr2b_row = gstr2b_rows[g_idx]
            if not _amounts_close(ledger_row.taxable_value, gstr2b_row.taxable_value):
                continue
            gstin_score = fuzz.ratio(ledger_row.gstin_normalized, gstr2b_row.gstin_normalized)
            if gstin_score < FUZZY_GSTIN_THRESHOLD:
                continue
            invoice_score = fuzz.ratio(
                ledger_row.invoice_number_normalized, gstr2b_row.invoice_number_normalized
            )
            if invoice_score < FUZZY_INVOICE_NUMBER_THRESHOLD:
                continue
            combined = (gstin_score + invoice_score) / 2
            if combined > best_score:
                best_idx, best_score = g_idx, combined

        if best_idx is not None:
            remaining_gstr2b.remove(best_idx)
            results.append(
                MatchResult(ledger_row, gstr2b_rows[best_idx], InvoiceMatchStatus.CLERICAL_MISMATCH)
            )
        else:
            still_unmatched_ledger.append(l_idx)

    # Pass 3: whatever's left in the ledger never shows up in GSTR-2B; whatever's
    # left in GSTR-2B was never in our purchase register.
    for l_idx in still_unmatched_ledger:
        results.append(MatchResult(ledger_rows[l_idx], None, InvoiceMatchStatus.MISSING_IN_GSTR2B))

    for g_idx in remaining_gstr2b:
        results.append(MatchResult(None, gstr2b_rows[g_idx], InvoiceMatchStatus.MISSING_IN_LEDGER))

    return results
