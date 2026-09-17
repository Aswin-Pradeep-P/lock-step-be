"""Turn a GSP (GST Suvidha Provider) sandbox GSTR-2B API response into the same
`CanonicalRow` shape the file-upload path produces, and fetch that response by
GSTIN + tax period.

This is the extension point `ingestion.py` was written to leave open ("a GSTN API
source can later produce the same CanonicalRow list without touching anything
downstream") — everything past this module (matching, risk rules, vendor scoring)
is identical whether a 2B came from an uploaded file or a live API fetch. Matching
stays 100% rule-based either way; nothing here decides a status.

The GSP envelope nests three "data" levels deep (outer transport envelope, then the
GSTN portal's own response envelope, then the actual payload) — that nesting is
GSTN's, not ours, and every GSP (Sandbox.co.in, ClearTax, ASP/GSPs in general) that
proxies the government API preserves it verbatim.
"""

from __future__ import annotations

from typing import Protocol

from lockstep.core.exceptions import IngestionError, ValidationError
from lockstep.services.ingestion import (
    CanonicalRow,
    clerical_key,
    normalize_gstin,
    normalize_invoice_number,
    normalize_name,
    parse_amount,
    parse_bool,
    parse_date,
)
from lockstep.services.mock_gstr2b import Gstr2bVariant, get_gstr2b_payload


def _dig(payload: dict, *keys: str) -> dict:
    node = payload
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            raise IngestionError(
                f"Unexpected GSP response shape: missing '{key}' "
                f"(looked for {'.'.join(keys)})"
            )
        node = node[key]
    return node


def parse_gsp_gstr2b_response(payload: dict) -> list[CanonicalRow]:
    """`docdata.b2b` (the per-invoice detail) is the source of truth here — `cpsumm`
    is only a per-vendor roll-up of the same numbers and is never read separately;
    everything vendor_scoring/dashboard need is derived from the row list either way.
    """
    inner = _dig(payload, "data", "data", "data")
    b2b_vendors = inner.get("docdata", {}).get("b2b", [])

    rows: list[CanonicalRow] = []
    for vendor in b2b_vendors:
        gstin = str(vendor.get("ctin", ""))
        vendor_name = str(vendor.get("trdnm", ""))
        supplier_filed_at = parse_date(str(vendor.get("supfildt", "")))

        for inv in vendor.get("inv", []):
            invoice_number = str(inv.get("inum", ""))
            rows.append(
                CanonicalRow(
                    row_index=len(rows),
                    invoice_number=invoice_number,
                    invoice_number_normalized=normalize_invoice_number(invoice_number),
                    clerical_key=clerical_key(invoice_number),
                    gstin=gstin,
                    gstin_normalized=normalize_gstin(gstin),
                    vendor_name=vendor_name,
                    vendor_name_normalized=normalize_name(vendor_name),
                    taxable_value=parse_amount(str(inv.get("txval", 0))),
                    igst=parse_amount(str(inv.get("igst", 0))),
                    cgst=parse_amount(str(inv.get("cgst", 0))),
                    sgst=parse_amount(str(inv.get("sgst", 0))),
                    cess=parse_amount(str(inv.get("cess", 0))),
                    invoice_date=parse_date(str(inv.get("dt", ""))),
                    supplier_filed_at=supplier_filed_at,
                    itc_available=parse_bool(str(inv.get("itcavl", ""))),
                    itc_reason=str(inv.get("rsn", "")),
                    is_reverse_charge=parse_bool(str(inv.get("rev", ""))) or False,
                    # IMS status (accept/reject/pending/no-action) is preserved in
                    # `raw` but deliberately not surfaced further than that: this
                    # sample never shows anything but "N", and a genuinely Pending
                    # invoice wouldn't appear in this feed at all to have a status
                    # read from — modeling that properly needs a real response that
                    # actually contains a rejected/pending section to test against.
                    raw={**vendor, "inv": inv},  # this invoice's own line, not siblings
                )
            )
    return rows


class GSPClient(Protocol):
    """A source of GSTR-2B data by GSTIN + tax period. Swapping the implementation
    (stub -> a real GSP subscription) never touches `parse_gsp_gstr2b_response` or
    anything downstream of it — both hand back the same envelope shape."""

    async def fetch_gstr2b(
        self, gstin: str, tax_period: str, variant: Gstr2bVariant = "corrected",
    ) -> dict: ...


class StubGSPClient:
    """Returns a GSTN-shaped sandbox payload regardless of the GSTIN/period asked
    for. `corrected` is the sample as-filed; `inconsistent` seeds demo mismatches.
    Swap in a real `GSPClient` once a GSP subscription exists — the envelope stays.
    """

    async def fetch_gstr2b(
        self, gstin: str, tax_period: str, variant: Gstr2bVariant = "corrected",
    ) -> dict:
        del gstin, tax_period
        return get_gstr2b_payload(variant)


def get_gsp_client() -> GSPClient:
    from lockstep.config import get_settings

    settings = get_settings()
    if settings.gsp_provider == "stub":
        return StubGSPClient()
    raise ValidationError(
        f"No GSP client configured for provider '{settings.gsp_provider}' — "
        f"only 'stub' is implemented so far."
    )
