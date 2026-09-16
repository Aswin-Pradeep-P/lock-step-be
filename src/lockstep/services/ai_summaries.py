"""The AI layer, deliberately narrow: one summary per affected vendor.

Per invoice would be ~500 calls for a 500-row ledger and 500 blurbs nobody reads.
Per vendor is ~20 calls and it is the thing someone actually acts on.

Nothing here decides a status, an amount or a date — those are rules, and a CA has to
be able to audit them. The model is handed already-computed facts and asked to phrase
them. Without an API key it falls back to a deterministic sentence built from the same
facts, so the product still works.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lockstep.config import Settings, get_settings
from lockstep.models.enums import AT_RISK_STATUSES
from lockstep.models.invoice import Invoice
from lockstep.services.periods import days_to_cutoff
from lockstep.services.vendor_scoring import VendorRiskRow

_SYSTEM = (
    "You write one short briefing for an Indian accountant about a single supplier's "
    "GST filing behaviour. Two or three sentences, plain English, no headings, no "
    "bullet points, no markdown. Use only the facts given — never invent an amount, a "
    "date or a count. Amounts are already formatted; repeat them exactly. End with what "
    "to do before the 13th."
)


def format_inr(amount: Decimal) -> str:
    """Indian shorthand: ₹1.84L, ₹4.2Cr. Headline figures only."""
    amount = Decimal(amount or 0)
    if amount >= 10_000_000:
        return f"₹{amount / 10_000_000:.2f}Cr"
    if amount >= 100_000:
        return f"₹{amount / 100_000:.2f}L"
    return f"₹{amount:,.0f}"


def _facts(vendor: VendorRiskRow, tax_period: str | None) -> list[str]:
    facts = [
        f"Supplier: {vendor.name}",
        f"Invoices missing from this period's GSTR-2B: {vendor.missing_invoice_count}",
        f"ITC at risk: {format_inr(vendor.current_exposure)}",
    ]
    if vendor.periods_observed:
        late = vendor.periods_observed - round(
            (vendor.on_time_rate or 0) * vendor.periods_observed
        )
        facts.append(
            f"Filed late in {late} of the last {vendor.periods_observed} periods observed"
        )
    if vendor.typical_filing_day:
        facts.append(f"Typically files around day {vendor.typical_filing_day} of the month")
    if vendor.avg_days_past_cutoff is not None:
        facts.append(
            f"Average {vendor.avg_days_past_cutoff:.1f} days past the 13th cutoff when late"
        )
    facts.append(f"Has filed this period: {'yes' if vendor.filed_this_period else 'no'}")
    if tax_period:
        days = days_to_cutoff(tax_period)
        facts.append(
            f"Days until the 13th cutoff: {days}"
            if days >= 0
            else f"The 13th cutoff passed {abs(days)} days ago"
        )
    return facts


def fallback_summary(vendor: VendorRiskRow, tax_period: str | None) -> str:
    """Deterministic. Same facts, no model — used when no API key is configured."""
    parts = [
        f"{vendor.name} has {vendor.missing_invoice_count} invoice"
        f"{'s' if vendor.missing_invoice_count != 1 else ''} worth "
        f"{format_inr(vendor.current_exposure)} missing from this period's GSTR-2B."
    ]
    if vendor.periods_observed:
        late = vendor.periods_observed - round(
            (vendor.on_time_rate or 0) * vendor.periods_observed
        )
        history = (
            f"They filed late in {late} of the last {vendor.periods_observed} periods"
            if late
            else f"They filed on time in all {vendor.periods_observed} periods observed"
        )
        if vendor.typical_filing_day:
            history += f", typically around the {vendor.typical_filing_day}th"
        parts.append(history + ".")
    if tax_period:
        days = days_to_cutoff(tax_period)
        parts.append(
            f"Contact them in the next {days} day{'s' if days != 1 else ''} or this credit "
            f"slips to next month."
            if days >= 0
            else "The cutoff has passed for this period — this credit has slipped to next month."
        )
    return " ".join(parts)


async def _call_anthropic(settings: Settings, user_content: str) -> str | None:
    if not settings.anthropic_api_key:
        return None
    try:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=250,
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        return text or None
    except Exception:
        return None


async def _call_groq(settings: Settings, user_content: str) -> str | None:
    """Same prompt, same contract, an OpenAI-compatible chat-completions call — used
    only when there's no working Anthropic key. Matching stays rule-only regardless
    of which provider narrates; this never sees or changes a status."""
    if not settings.groq_api_key:
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                json={
                    "model": settings.groq_model,
                    "max_tokens": 250,
                    # The default model is a reasoning model — without this, its own
                    # reasoning tokens can eat the entire max_tokens budget before
                    # any visible text comes out (confirmed: ~260 reasoning tokens
                    # at default effort vs ~25 at "low", for a task this short).
                    "reasoning_effort": "low",
                    "messages": [
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": user_content},
                    ],
                },
            )
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"].strip()
            return text or None
    except Exception:
        return None


async def vendor_summary(
    db: AsyncSession, vendor: VendorRiskRow, tax_period: str | None
) -> str:
    settings = get_settings()
    user_content = "\n".join(_facts(vendor, tax_period))

    for provider in (_call_anthropic, _call_groq):
        text = await provider(settings, user_content)
        if text:
            return text

    # A summary is never worth failing a reconciliation over.
    return fallback_summary(vendor, tax_period)


async def draft_vendor_email(
    db: AsyncSession, vendor: VendorRiskRow, period_id, tax_period: str | None
) -> dict:
    """Generate and store the draft. We never actually send — the action is logged."""
    from lockstep.services.dashboard import latest_check_id

    check_id = await latest_check_id(db, period_id)
    invoices = (
        await db.execute(
            select(Invoice).where(
                Invoice.vendor_id == vendor.vendor_id,
                Invoice.period_id == period_id,
                Invoice.check_id == check_id,
                Invoice.status.in_(AT_RISK_STATUSES),
                # An AMOUNT_MISMATCH pair writes both sides with the same status;
                # without this an invoice with a mismatch is listed twice, once per
                # side, each with a different tax figure (see vendor_scoring's
                # matching guard on the same query shape).
                Invoice.source != "GSTR2B",
            )
        )
    ).scalars().all()

    lines = [
        f"- {i.invoice_number} dated {i.invoice_date or 'n/a'}: "
        f"₹{i.total_tax:,.2f} of tax"
        for i in invoices
    ]
    days = days_to_cutoff(tax_period) if tax_period else None
    deadline = (
        f"Please file before the 13th — {days} day{'s' if days != 1 else ''} from today."
        if days is not None and days >= 0
        else "Please file at the earliest."
    )
    body = (
        f"Dear {vendor.name},\n\n"
        f"The following invoices are in our purchase records but have not yet appeared in "
        f"our GSTR-2B, which suggests they are not yet reported in your GSTR-1:\n\n"
        + "\n".join(lines)
        + f"\n\n{deadline} If they miss the cutoff, the input tax credit of "
        f"{format_inr(vendor.current_exposure)} moves to the next period for us.\n\n"
        f"If you have already filed, please share the ARN and ignore this note.\n\n"
        f"Regards"
    )
    # A drafted email with nowhere to send it is not a success — surfacing that
    # explicitly is what lets the UI show "vendor info for reminder not present"
    # instead of a button that silently does nothing.
    can_send = bool(vendor.contact_email)
    return {
        "to": vendor.contact_email,
        "subject": f"GSTR-1 filing: {len(invoices)} invoice(s) pending before the 13th",
        "body": body,
        "invoice_numbers": [i.invoice_number for i in invoices],
        "can_send": can_send,
        "reason": (
            None if can_send
            else "No email on file for this vendor — cannot send a reminder."
        ),
    }
