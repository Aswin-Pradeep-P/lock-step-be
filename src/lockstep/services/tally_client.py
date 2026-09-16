"""Tally XML client — ported from server/lib/tally-client.ts.

Sends XML envelopes to a local TallyPrime HTTP server and parses the response.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import httpx

from lockstep.schemas.fe_types import PurchaseRecordIn


def build_company_list_xml() -> str:
    return """<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>List of Companies</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      </STATICVARIABLES>
    </DESC>
  </BODY>
</ENVELOPE>"""


def build_purchase_voucher_xml(company: str, from_date: str, to_date: str) -> str:
    return f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Data</TYPE>
    <ID>Voucher Register</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
        <SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>
        <SVFROMDATE TYPE="DATE">{from_date}</SVFROMDATE>
        <SVTODATE TYPE="DATE">{to_date}</SVTODATE>
        <VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>
      </STATICVARIABLES>
    </DESC>
  </BODY>
</ENVELOPE>"""


async def post_to_tally(host: str, port: int, xml_body: str) -> str:
    url = f"http://{host}:{port}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                url,
                content=xml_body,
                headers={"Content-Type": "text/xml"},
            )
            return resp.text
        except httpx.ConnectError:
            raise ConnectionError(
                f"Cannot connect to TallyPrime at {host}:{port}. "
                f"Make sure TallyPrime is running with the HTTP server enabled."
            )
        except httpx.TimeoutException:
            raise ConnectionError(
                f"Connection to TallyPrime at {host}:{port} timed out. "
                f"Check the host and port."
            )


def _check_tally_error(root: ET.Element) -> None:
    for tag in ("BODY/DATA/LINEERROR", "BODY/LINEERROR"):
        elem = root.find(tag)
        if elem is not None and elem.text:
            raise ValueError(f"TallyPrime error: {elem.text}")


def _text(elem: ET.Element | None) -> str:
    return (elem.text or "").strip() if elem is not None else ""


def _num(elem: ET.Element | None) -> float:
    if elem is None:
        return 0.0
    try:
        return abs(float(elem.text or 0))
    except (ValueError, TypeError):
        return 0.0


def _format_tally_date(raw: str) -> str:
    s = re.sub(r"[^0-9]", "", raw)
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return raw


def parse_company_list(xml: str) -> list[str]:
    root = ET.fromstring(xml)
    _check_tally_error(root)

    companies: list[str] = []
    for collection in root.iter("COLLECTION"):
        for company in collection.iter("COMPANY"):
            name_elem = company.find("NAME")
            name = _text(name_elem) if name_elem is not None else _text(company)
            if name:
                companies.append(name)

    return companies


def parse_purchase_vouchers(xml: str) -> list[PurchaseRecordIn]:
    root = ET.fromstring(xml)
    _check_tally_error(root)

    records: list[PurchaseRecordIn] = []

    for voucher in root.iter("VOUCHER"):
        voucher_type = _text(voucher.find("VOUCHERTYPENAME"))
        if voucher_type.lower() != "purchase":
            continue

        igst_input = 0.0
        cgst_input = 0.0
        sgst_input = 0.0
        gross_total = _num(voucher.find("AMOUNT"))

        for entry in voucher.iter("LEDGERENTRIES.LIST"):
            name = _text(entry.find("LEDGERNAME")).lower()
            amount = _num(entry.find("AMOUNT"))
            if "igst" in name:
                igst_input = amount
            elif "cgst" in name:
                cgst_input = amount
            elif "sgst" in name:
                sgst_input = amount

        date_raw = _text(voucher.find("DATE")) or _text(voucher.find("EFFECTIVEDATE"))
        ref_date_raw = _text(voucher.find("REFERENCEDATE")) or _text(voucher.find("DATE"))

        records.append(PurchaseRecordIn(
            date=_format_tally_date(date_raw),
            particulars=_text(voucher.find("NARRATION")) or _text(voucher.find("PARTYLEDGERNAME")),
            supplier=_text(voucher.find("PARTYLEDGERNAME")),
            voucher_type=voucher_type,
            voucher_no=_text(voucher.find("VOUCHERNUMBER")),
            voucher_ref_no=(
                _text(voucher.find("REFERENCE"))
                or _text(voucher.find("INVOICENUMBER"))
                or _text(voucher.find("VOUCHERNUMBER"))
            ),
            voucher_ref_date=_format_tally_date(ref_date_raw),
            narration=_text(voucher.find("NARRATION")),
            gross_total=gross_total,
            igst_input=igst_input,
            cgst_input=cgst_input,
            sgst_input=sgst_input,
        ))

    return records
