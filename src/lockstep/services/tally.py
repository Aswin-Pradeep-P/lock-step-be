"""Read a purchase register straight out of TallyPrime, as `CanonicalRow`s.

The ledger-side mirror of `gsp.py`: that module replaces the uploaded GSTR-2B with a
live API fetch, this one replaces the uploaded Tally export with a live fetch from the
running TallyPrime. Both hand back the same `CanonicalRow` list, so matching, risk
rules and vendor scoring are identical no matter where the rows came from — and
matching stays 100% rule-based either way. Nothing here decides a status.

TallyPrime speaks XML over plain HTTP on port 9000 when "Act as Server" is enabled
(Gateway of Tally -> F1 Help -> Settings -> Connectivity -> Client/Server
configuration). The browser cannot call it directly — it is a desktop service with no
CORS — so the request goes through this backend, which runs on the same machine.

Two Tally realities shape the parsing:

1. **A purchase voucher carries the supplier's document number in `REFERENCE`, not in
   `VOUCHERNUMBER`.** `VOUCHERNUMBER` is our own internal voucher, which GSTR-2B has
   never seen. This is the exact same trap the CSV export has (see
   `ingestion.CANONICAL_ALIASES`), in a different costume.
2. **The party GSTIN usually lives on the ledger master, not on the voucher.** Where
   Tally does include it we take it; otherwise the row is left GSTIN-less and
   `matching.resolve_missing_gstins` resolves it by party name, exactly as it does for
   a GSTIN-less CSV.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from xml.etree import ElementTree

import httpx

from lockstep.core.exceptions import IngestionError
from lockstep.services.ingestion import (
    CanonicalRow,
    clerical_key,
    normalize_gstin,
    normalize_invoice_number,
    normalize_name,
    parse_amount,
    parse_date,
)

#: Tally dates on the wire are YYYYMMDD.
_TALLY_DATE = re.compile(r"^\d{8}$")

#: Ledger names Tally uses for each tax head. Matched case-insensitively as a
#: substring, because users rename these freely ("IGST", "Input IGST @ 18%", ...).
_TAX_LEDGER_HINTS = {
    "igst": ("igst", "integrated tax", "integrated gst"),
    "cgst": ("cgst", "central tax", "central gst"),
    "sgst": ("sgst", "state tax", "state gst", "utgst"),
    "cess": ("cess",),
}


def _text(node: ElementTree.Element | None, *paths: str) -> str:
    """First non-empty value among `paths`, searched under `node`."""
    if node is None:
        return ""
    for path in paths:
        found = node.find(path)
        if found is not None and (found.text or "").strip():
            return (found.text or "").strip()
    return ""


def _tally_date(raw: str):
    if _TALLY_DATE.match(raw or ""):
        return parse_date(f"{raw[:4]}-{raw[4:6]}-{raw[6:]}")
    return parse_date(raw)


def _classify_tax_ledger(ledger_name: str) -> str | None:
    lowered = ledger_name.lower()
    for head, hints in _TAX_LEDGER_HINTS.items():
        if any(hint in lowered for hint in hints):
            return head
    return None


def build_company_list_request() -> str:
    return (
        "<ENVELOPE>"
        "<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        "<TYPE>Collection</TYPE><ID>List of Companies</ID></HEADER>"
        "<BODY><DESC><STATICVARIABLES>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        "</STATICVARIABLES></DESC></BODY>"
        "</ENVELOPE>"
    )


def build_purchase_register_request(company: str, from_date: str, to_date: str) -> str:
    """`from_date`/`to_date` are YYYYMMDD, the only format Tally accepts here."""
    return (
        "<ENVELOPE>"
        "<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        "<TYPE>Data</TYPE><ID>Voucher Register</ID></HEADER>"
        "<BODY><DESC><STATICVARIABLES>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        f"<SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>"
        f'<SVFROMDATE TYPE="DATE">{from_date}</SVFROMDATE>'
        f'<SVTODATE TYPE="DATE">{to_date}</SVTODATE>'
        "<VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>"
        "</STATICVARIABLES></DESC></BODY>"
        "</ENVELOPE>"
    )


#: XML 1.0 forbids most control characters, but Tally emits them anyway — a real
#: export carries "&#4; Not Applicable" in CSTFORMISSUETYPE, which makes a strict
#: parser reject the entire 1MB response. Legal: #x9, #xA, #xD, #x20-#xD7FF,
#: #xE000-#xFFFD, #x10000-#x10FFFF.
_ILLEGAL_CHAR_REF = re.compile(r"&#(\d+);")
_ILLEGAL_RAW = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _is_legal_xml_char(code: int) -> bool:
    return (
        code in (0x9, 0xA, 0xD)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= 0x10FFFF
    )


def sanitize_tally_xml(xml: str) -> str:
    """Strip the control characters Tally emits that XML 1.0 does not allow."""
    cleaned = _ILLEGAL_CHAR_REF.sub(
        lambda m: m.group(0) if _is_legal_xml_char(int(m.group(1))) else "", xml
    )
    return _ILLEGAL_RAW.sub("", cleaned)


def _parse_envelope(xml: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(sanitize_tally_xml(xml))
    except ElementTree.ParseError as exc:
        raise IngestionError(f"TallyPrime returned XML we could not parse: {exc}") from exc


def parse_company_list(xml: str) -> list[str]:
    root = _parse_envelope(xml)
    names = []
    for company in root.iter("COMPANY"):
        name = (company.get("NAME") or _text(company, "NAME")).strip()
        if name and name not in names:
            names.append(name)
    return names


def parse_purchase_vouchers(xml: str) -> list[CanonicalRow]:
    """Turn a Tally Voucher Register export into canonical rows.

    A voucher with no supplier reference and no party is skipped rather than guessed
    at — a row we cannot key on would otherwise read as MISSING_IN_GSTR2B and inflate
    the exposure figure with noise.
    """
    root = _parse_envelope(xml)
    rows: list[CanonicalRow] = []

    for index, voucher in enumerate(root.iter("VOUCHER")):
        party = _text(
            voucher, "PARTYLEDGERNAME", "PARTYNAME", "BASICBUYERNAME", "LEDGERNAME"
        )
        # The supplier's own document number — never VOUCHERNUMBER, which is ours.
        invoice_number = _text(voucher, "REFERENCE", "REFERENCENUMBER")
        if not invoice_number and not party:
            continue

        invoice_date = _tally_date(
            _text(voucher, "REFERENCEDATE") or _text(voucher, "DATE", "EFFECTIVEDATE")
        )

        taxes = {"igst": Decimal("0"), "cgst": Decimal("0"), "sgst": Decimal("0"),
                 "cess": Decimal("0")}
        taxable = Decimal("0")
        # Tally writes ALLLEDGERENTRIES.LIST for an invoice-view purchase voucher and
        # LEDGERENTRIES.LIST for the simpler voucher view. Real purchase bills are
        # almost always the former, so both have to be read.
        entries = list(voucher.iter("ALLLEDGERENTRIES.LIST")) or list(
            voucher.iter("LEDGERENTRIES.LIST")
        )
        for entry in entries:
            ledger_name = _text(entry, "LEDGERNAME")
            # Tally signs a purchase's ledger amounts negative; we want magnitudes.
            amount = abs(parse_amount(_text(entry, "AMOUNT")))
            head = _classify_tax_ledger(ledger_name)
            if head:
                taxes[head] += amount
            elif normalize_name(ledger_name) != normalize_name(party):
                # Anything that is neither a tax head nor the party ledger is stock
                # or expense value — that is the taxable base.
                taxable += amount

        if taxable == 0:
            taxable = abs(parse_amount(_text(voucher, "AMOUNT", "BASICAMOUNT")))

        gstin = normalize_gstin(
            _text(voucher, "PARTYGSTIN", "CONSIGNEEGSTIN", "BASICBUYERGSTIN")
        )

        rows.append(
            CanonicalRow(
                row_index=index,
                invoice_number=invoice_number,
                invoice_number_normalized=normalize_invoice_number(invoice_number),
                clerical_key=clerical_key(invoice_number),
                gstin=gstin,
                gstin_normalized=gstin,
                vendor_name=party,
                vendor_name_normalized=normalize_name(party),
                taxable_value=taxable,
                igst=taxes["igst"],
                cgst=taxes["cgst"],
                sgst=taxes["sgst"],
                cess=taxes["cess"],
                invoice_date=invoice_date,
                description=_text(voucher, "NARRATION"),
                raw={
                    "Voucher No.": _text(voucher, "VOUCHERNUMBER"),
                    "Voucher Ref. No.": invoice_number,
                    "Party": party,
                    "Date": _text(voucher, "DATE"),
                    "Source": "TallyPrime",
                },
            )
        )

    return rows


@dataclass
class TallyStatus:
    reachable: bool
    companies: list[str]
    detail: str


class TallyClient(Protocol):
    """A source of purchase vouchers from a running TallyPrime."""

    async def status(self) -> TallyStatus: ...

    async def fetch_purchase_register(
        self, company: str, from_date: str, to_date: str
    ) -> list[CanonicalRow]: ...


class HttpTallyClient:
    """Talks to a real TallyPrime over its XML-over-HTTP interface."""

    def __init__(self, host: str, port: int, timeout: float = 30.0) -> None:
        self._url = f"http://{host}:{port}"
        self._timeout = timeout

    async def _post(self, xml: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    self._url, content=xml.encode("utf-8"),
                    headers={"Content-Type": "text/xml;charset=utf-8"},
                )
                response.raise_for_status()
                return response.text
        except httpx.HTTPError as exc:
            raise IngestionError(
                f"Could not reach TallyPrime at {self._url}. Is it running, with "
                f"'Act as Server' enabled under F1 Help -> Settings -> Connectivity? "
                f"({exc})"
            ) from exc

    async def status(self) -> TallyStatus:
        try:
            companies = parse_company_list(await self._post(build_company_list_request()))
        except IngestionError as exc:
            return TallyStatus(reachable=False, companies=[], detail=str(exc))
        return TallyStatus(
            reachable=True,
            companies=companies,
            detail=(
                f"Connected to TallyPrime at {self._url}."
                if companies
                else f"TallyPrime is running at {self._url} but has no company open."
            ),
        )

    async def fetch_purchase_register(
        self, company: str, from_date: str, to_date: str
    ) -> list[CanonicalRow]:
        xml = await self._post(
            build_purchase_register_request(company, from_date, to_date)
        )
        return parse_purchase_vouchers(xml)


def get_tally_client() -> TallyClient:
    from lockstep.config import get_settings

    settings = get_settings()
    return HttpTallyClient(settings.tally_host, settings.tally_port)
