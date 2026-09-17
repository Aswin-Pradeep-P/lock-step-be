"""Per-invoice AI insight: a richer reason and a concrete next step.

Generated on demand (an invoice detail is opened) and cached on the row, never in
bulk during a check — the same "narrative is a courtesy, not a gate" contract as
`ai_summaries.py`, at invoice scale instead of vendor scale. Nothing here can
change a `status` or a `match_reason`: those stay rule-only and auditable (see
`matching.py`). This only adds a second, clearly-labelled AI-authored layer for a
human to read and verify — never a replacement for the deterministic fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from lockstep.config import get_settings
from lockstep.models.invoice import Invoice
from lockstep.services import llm
from lockstep.services.itc_reference import SECTION_17_5_REFERENCE
from lockstep.services.risk_rules import sec17_5_hint

_REASON_HEADER = "### Reason"
_SUGGESTION_HEADER = "### Suggested Action"

_SYSTEM = f"""\
You are briefing an Indian chartered accountant on ONE invoice from a GST purchase \
reconciliation. You are given the facts already established by deterministic rules \
(the matched status, amounts, dates) — never invent or contradict a fact, an \
amount, a date or a count; repeat figures exactly as given.

Reply in exactly this markdown shape, nothing before or after:

{_REASON_HEADER}
2-4 sentences in plain English explaining why this invoice landed in this status. \
If the invoice description plausibly falls under a Section 17(5) blocked category \
below, name the specific clause letter and note any exception that might still \
apply — phrase this as "may be blocked under clause (b) — verify..." not as a \
certainty; a CA must independently confirm any legal conclusion. If nothing in \
Section 17(5) plausibly applies, do not mention it.

{_SUGGESTION_HEADER}
One concrete, actionable next step as a short markdown bullet list (1-3 bullets) \
telling the accountant exactly what to do to resolve this invoice or make its ITC \
safe to claim — e.g. contact the vendor, correct a specific field, verify a \
statutory exception, or write off the credit as non-recoverable.

Reference — Section 17(5), CGST Act 2017 (cite a clause only if it genuinely applies):
{SECTION_17_5_REFERENCE}
"""


@dataclass
class InvoiceInsight:
    reason_md: str
    suggestion_md: str


def _money(amount: Decimal | None) -> str:
    return f"₹{amount:,.2f}" if amount is not None else "n/a"


def _facts(invoice: Invoice, vendor_name: str | None) -> list[str]:
    facts = [
        f"Status: {invoice.status}",
        f"Deterministic reason already established: {invoice.match_reason or 'none recorded'}",
        f"Invoice number: {invoice.invoice_number}",
        f"Vendor: {vendor_name or invoice.vendor_gstin or 'unknown'}",
        f"Invoice date: {invoice.invoice_date or 'n/a'}",
        f"Taxable value: {_money(invoice.taxable_value)}",
        f"Total tax (IGST+CGST+SGST+Cess): {_money(invoice.total_tax)}",
        f"Reverse charge: {'yes' if invoice.is_reverse_charge else 'no'}",
    ]
    if invoice.description:
        facts.append(f"Invoice/line description: {invoice.description}")
    if invoice.itc_available is not None:
        availability = "available" if invoice.itc_available else "not available"
        facts.append(
            f"GSTR-2B ITC Availability flag: {availability}"
            + (f" ({invoice.itc_reason})" if invoice.itc_reason else "")
        )
    if invoice.supplier_filed_at:
        facts.append(f"Supplier filed GSTR-1 on: {invoice.supplier_filed_at}")
    if invoice.recoverable_until:
        facts.append(f"Sec 16(4) recovery deadline: {invoice.recoverable_until}")
    return facts


def _parse(text: str) -> InvoiceInsight | None:
    if _REASON_HEADER not in text or _SUGGESTION_HEADER not in text:
        return None
    reason_part, _, suggestion_part = text.partition(_SUGGESTION_HEADER)
    reason_md = reason_part.split(_REASON_HEADER, 1)[-1].strip()
    suggestion_md = suggestion_part.strip()
    if not reason_md or not suggestion_md:
        return None
    return InvoiceInsight(reason_md=reason_md, suggestion_md=suggestion_md)


_SUGGESTION_BY_STATUS = {
    "MISSING_IN_GSTR2B": (
        "- Contact the vendor and ask them to file this invoice in their next GSTR-1 "
        "before the 13th, so it appears in GSTR-2B and the credit becomes claimable."
    ),
    "MISSING_IN_LEDGER": (
        "- Verify whether this is an unrecorded purchase (add it to your books) or "
        "an invoice filed against your GSTIN by mistake (ask the vendor to amend it)."
    ),
    "AMOUNT_MISMATCH": (
        "- Reconcile the tax figure directly with the vendor and, once agreed, "
        "correct whichever side (your books or their GSTR-1) is wrong."
    ),
    "CLERICAL_MISMATCH": (
        "- Confirm the correct invoice number with the vendor and correct the typo "
        "in your books — no further action needed once the numbers agree."
    ),
    "ITC_INELIGIBLE": (
        "- Do not claim this credit. Write it off as non-recoverable input tax and "
        "exclude it from this period's ITC claim."
    ),
    "DUPLICATE": (
        "- Remove the duplicate entry from your purchase ledger before filing; only "
        "one of the two rows represents a real purchase."
    ),
}


def deterministic_insight(invoice: Invoice) -> InvoiceInsight:
    """No model configured, or the model failed — same facts, no AI."""
    status = str(invoice.status)
    reason = invoice.match_reason or f"This invoice is in status {status}."
    if status == "MISSING_IN_GSTR2B" and sec17_5_hint(invoice.description or ""):
        reason += (
            " The description also resembles a category commonly blocked under "
            "Section 17(5) (see clause (b) above) — verify this independently of "
            "whether the vendor ever files it."
        )
    suggestion = _SUGGESTION_BY_STATUS.get(
        status, "- Review this invoice manually; no automatic suggestion applies to its status."
    )
    return InvoiceInsight(reason_md=reason, suggestion_md=suggestion)


async def generate_insight(invoice: Invoice, vendor_name: str | None) -> InvoiceInsight:
    settings = get_settings()
    user_content = "\n".join(_facts(invoice, vendor_name))

    text = await llm.complete(settings, _SYSTEM, user_content, max_tokens=400)
    parsed = _parse(text) if text else None
    return parsed or deterministic_insight(invoice)
