import uuid
from decimal import Decimal

from lockstep.models.enums import InvoiceMatchStatus, InvoiceSource
from lockstep.models.invoice import Invoice
from lockstep.services.invoice_insight import _parse, deterministic_insight


def _invoice(**overrides) -> Invoice:
    defaults = dict(
        id=uuid.uuid4(),
        check_id=uuid.uuid4(),
        period_id=uuid.uuid4(),
        source=InvoiceSource.LEDGER,
        invoice_number="INV/1",
        status=InvoiceMatchStatus.MISSING_IN_GSTR2B,
        match_reason="Invoice INV/1 has not appeared in GSTR-2B.",
        taxable_value=Decimal("10000"),
        igst=Decimal("1800"),
        cgst=Decimal("0"),
        sgst=Decimal("0"),
        cess=Decimal("0"),
        is_reverse_charge=False,
        raw_data={},
    )
    defaults.update(overrides)
    return Invoice(**defaults)


def test_deterministic_insight_flags_blocked_category_for_missing_invoice():
    invoice = _invoice(description="Motor vehicle lease for sales team")
    insight = deterministic_insight(invoice)
    assert "Section 17(5)" in insight.reason_md
    assert "clause (b)" in insight.reason_md
    assert "vendor" in insight.suggestion_md.lower()


def test_deterministic_insight_does_not_flag_ordinary_purchase():
    invoice = _invoice(description="Steel rods for factory floor")
    insight = deterministic_insight(invoice)
    assert "Section 17(5)" not in insight.reason_md
    assert insight.reason_md == invoice.match_reason


def test_deterministic_insight_suggestion_matches_status():
    invoice = _invoice(status=InvoiceMatchStatus.DUPLICATE, match_reason="Duplicate entry.")
    insight = deterministic_insight(invoice)
    assert "duplicate" in insight.suggestion_md.lower()


def test_deterministic_insight_falls_back_for_unmapped_status():
    invoice = _invoice(status=InvoiceMatchStatus.PENDING, match_reason=None)
    insight = deterministic_insight(invoice)
    assert "PENDING" in insight.reason_md
    assert insight.suggestion_md  # never empty


def test_parse_extracts_both_sections():
    text = (
        "### Reason\nThe vendor has not filed this invoice.\n\n"
        "### Suggested Action\n- Contact the vendor."
    )
    parsed = _parse(text)
    assert parsed is not None
    assert parsed.reason_md == "The vendor has not filed this invoice."
    assert parsed.suggestion_md == "- Contact the vendor."


def test_parse_returns_none_when_headers_missing():
    assert _parse("Just some free text with no headers.") is None
