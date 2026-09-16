"""Per-invoice risk classification + markdown summary via Claude.

Deterministic facts (match status, Sec 16(4) deadline/days-remaining, vendor
history) are computed elsewhere and handed to the model as context — the
model explains, cites, and makes the Sec 17(5) eligibility call from the
invoice description; it never computes dates itself.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

import anthropic
from pydantic import BaseModel

from lockstep.config import get_settings
from lockstep.models.enums import InvoiceRiskTier

logger = logging.getLogger(__name__)
settings = get_settings()

_client: anthropic.AsyncAnthropic | None = None


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


class InvoiceRiskAssessment(BaseModel):
    risk_tier: Literal["low_risk", "high_risk", "blocked"]
    reason: str
    citations: list[str]
    recoverable: bool
    markdown_summary: str


SYSTEM_PROMPT = """\
You are a GST (Indian Goods & Services Tax) compliance analyst. You assess one \
purchase invoice at a time against India's GSTR-2B reconciliation rules and \
explain the result to a finance team.

Ground every citation in these rules — do not invent section numbers:
- Rule 88D / Rule 60, CGST Rules: since April 2026, GSTR-3B filing is hard-blocked \
at submission if claimed ITC exceeds GSTR-2B. This is why every mismatch matters.
- Section 16(4), CGST Act: ITC for a financial year must be claimed by 30 November \
of the following financial year (or the annual return filing date, if earlier). \
Once this window closes, the credit is permanently lost — no matter whose fault \
the mismatch was.
- Section 17(5), CGST Act: certain categories are never eligible for ITC \
regardless of filing status — e.g. food & beverages, outdoor catering, health \
and life insurance (unless obligatory), club memberships, employee travel \
benefits (leave travel), rent-a-cab/motor vehicle hire (with narrow exceptions), \
and works contract services for immovable property construction.

You will be given: the match outcome (already computed, do not re-derive it), \
the invoice's description/line-item text, the deterministic Sec 16(4) deadline \
and days remaining (already computed — treat as fact), and the vendor's recent \
filing history. Decide:
- "blocked": the expense category is Sec 17(5)-ineligible (from the description), \
OR the Sec 16(4) window has already closed. Never recoverable.
- "high_risk": the ITC is missing from GSTR-2B (vendor hasn't filed it) but the \
Sec 16(4) window is still open — recoverable if the vendor files in time.
- "low_risk": a clerical/typo mismatch (GSTIN or invoice number) that is almost \
certainly a data-entry issue, not a compliance problem.

Write `reason` as one or two plain sentences a finance analyst can act on. \
`citations` is a list of the specific rule/section references you relied on \
(e.g. "Section 16(4), CGST Act"). `markdown_summary` is a short markdown block \
(a few lines) suitable for display in a table cell: state the issue, the \
citation, and — if recoverable — the exact deadline and days remaining.
"""


def _user_prompt(context: dict) -> str:
    lines = [
        f"Invoice number: {context['invoice_number']}",
        f"Vendor: {context['vendor_name']} (GSTIN {context['gstin']})",
        f"Taxable value: ₹{context['taxable_value']:,.2f}",
        f"Line-item description: {context['description'] or '(none provided)'}",
        f"Match outcome (already determined): {context['match_status']}",
    ]
    if context.get("gstr2b_counterpart"):
        lines.append(f"Closest GSTR-2B counterpart: {context['gstr2b_counterpart']}")
    if context.get("recoverable_until"):
        lines.append(
            f"Sec 16(4) deadline (already computed): {context['recoverable_until']} "
            f"({context['days_remaining']} days remaining, window "
            f"{'OPEN' if context['days_remaining'] > 0 else 'CLOSED'})"
        )
    if context.get("sec17_5_hint"):
        lines.append(
            "Heuristic flag: the description matched a keyword commonly associated "
            "with a Sec 17(5)-ineligible category — verify and decide."
        )
    if context.get("vendor_history"):
        lines.append(f"Vendor's recent filing history: {context['vendor_history']}")
    return "\n".join(lines)


async def classify_invoice(context: dict) -> InvoiceRiskAssessment:
    client = get_client()
    try:
        response = await client.messages.parse(
            model=settings.anthropic_model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _user_prompt(context)}],
            output_format=InvoiceRiskAssessment,
        )
        return response.parsed_output
    except anthropic.RateLimitError:
        logger.warning("Anthropic rate limited for invoice %s", context.get("invoice_number"))
        raise
    except anthropic.APIStatusError as exc:
        logger.error("Anthropic API error for invoice %s: %s", context.get("invoice_number"), exc)
        raise
    except anthropic.APIConnectionError as exc:
        logger.error(
            "Anthropic connection error for invoice %s: %s", context.get("invoice_number"), exc
        )
        raise


def fallback_assessment(context: dict) -> InvoiceRiskAssessment:
    """Used only if the AI call fails after retries — keeps the run from failing wholesale.

    Falls back to the deterministic match status rather than guessing: a
    clerical typo is still low-risk even when the model can't be reached.
    """
    days_remaining = context.get("days_remaining")
    recoverable = days_remaining is None or days_remaining > 0

    if context.get("match_status") == "CLERICAL_MISMATCH":
        risk_tier = "low_risk"
    elif recoverable:
        risk_tier = "high_risk"
    else:
        risk_tier = "blocked"

    return InvoiceRiskAssessment(
        risk_tier=risk_tier,
        reason="AI summary could not be generated for this invoice; showing a fallback.",
        citations=[],
        recoverable=recoverable,
        markdown_summary="_AI summary unavailable — please retry this run or review manually._",
    )


async def classify_with_retry_and_fallback(
    context: dict, retries: int = 2
) -> InvoiceRiskAssessment:
    for attempt in range(retries + 1):
        try:
            return await classify_invoice(context)
        except (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError):
            if attempt == retries:
                return fallback_assessment(context)
            await asyncio.sleep(2**attempt)
    return fallback_assessment(context)  # unreachable, keeps type-checkers happy


SAFE_SUMMARY_MARKDOWN = (
    "**Matched.** Invoice number, GSTIN, and amount all agree with GSTR-2B. No action needed."
)
"""Used directly for EXACT_MATCH invoices — no AI call, nothing to explain."""

RISK_TIER_MAP: dict[str, InvoiceRiskTier] = {
    "low_risk": InvoiceRiskTier.LOW_RISK,
    "high_risk": InvoiceRiskTier.HIGH_RISK,
    "blocked": InvoiceRiskTier.BLOCKED,
}
