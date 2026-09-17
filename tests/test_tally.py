"""TallyPrime XML parsing.

The live connection is verified against the real TallyPrime (see
`services/tally.HttpTallyClient`); these tests pin the parsing, because a Tally
voucher hides the same two traps the CSV export does — the supplier's document number
is in REFERENCE not VOUCHERNUMBER, and the party GSTIN is usually absent entirely.
"""

from datetime import date
from decimal import Decimal

import pytest

from lockstep.core.exceptions import IngestionError
from lockstep.services.matching import resolve_missing_gstins
from lockstep.services.tally import (
    build_purchase_register_request,
    parse_company_list,
    parse_purchase_vouchers,
)

COMPANY_LIST_XML = """<ENVELOPE>
 <HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER>
 <BODY><DATA><COLLECTION>
   <COMPANY NAME="Acme Manufacturing Pvt Ltd" RESERVEDNAME=""/>
   <COMPANY NAME="New New" RESERVEDNAME=""/>
 </COLLECTION></DATA></BODY>
</ENVELOPE>"""

# Shaped after a real TallyPrime Voucher Register export: interstate (IGST) first,
# then intrastate (CGST+SGST). Tally signs purchase ledger amounts negative.
VOUCHER_XML = """<ENVELOPE>
 <HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER>
 <BODY><DATA>
  <TALLYMESSAGE>
   <VOUCHER VCHTYPE="Purchase" ACTION="Create">
    <DATE>20260805</DATE>
    <REFERENCE>CN/INV/2026/801</REFERENCE>
    <REFERENCEDATE>20260728</REFERENCEDATE>
    <VOUCHERNUMBER>PV-CN-001</VOUCHERNUMBER>
    <PARTYLEDGERNAME>CloudNine Technologies Pvt Ltd</PARTYLEDGERNAME>
    <NARRATION>Annual cloud hosting renewal</NARRATION>
    <LEDGERENTRIES.LIST>
     <LEDGERNAME>CloudNine Technologies Pvt Ltd</LEDGERNAME>
     <AMOUNT>236000.00</AMOUNT>
    </LEDGERENTRIES.LIST>
    <LEDGERENTRIES.LIST>
     <LEDGERNAME>Hosting Expenses</LEDGERNAME>
     <AMOUNT>-200000.00</AMOUNT>
    </LEDGERENTRIES.LIST>
    <LEDGERENTRIES.LIST>
     <LEDGERNAME>Input IGST @ 18%</LEDGERNAME>
     <AMOUNT>-36000.00</AMOUNT>
    </LEDGERENTRIES.LIST>
   </VOUCHER>
  </TALLYMESSAGE>
  <TALLYMESSAGE>
   <VOUCHER VCHTYPE="Purchase" ACTION="Create">
    <DATE>20260810</DATE>
    <REFERENCE>GP-2026-221</REFERENCE>
    <REFERENCEDATE>20260808</REFERENCEDATE>
    <VOUCHERNUMBER>PV-GP-002</VOUCHERNUMBER>
    <PARTYLEDGERNAME>GreenPack Solutions</PARTYLEDGERNAME>
    <PARTYGSTIN>29AAACG4455S1Z7</PARTYGSTIN>
    <LEDGERENTRIES.LIST>
     <LEDGERNAME>GreenPack Solutions</LEDGERNAME>
     <AMOUNT>118000.00</AMOUNT>
    </LEDGERENTRIES.LIST>
    <LEDGERENTRIES.LIST>
     <LEDGERNAME>Packing Material</LEDGERNAME>
     <AMOUNT>-100000.00</AMOUNT>
    </LEDGERENTRIES.LIST>
    <LEDGERENTRIES.LIST>
     <LEDGERNAME>Input CGST</LEDGERNAME>
     <AMOUNT>-9000.00</AMOUNT>
    </LEDGERENTRIES.LIST>
    <LEDGERENTRIES.LIST>
     <LEDGERNAME>Input SGST</LEDGERNAME>
     <AMOUNT>-9000.00</AMOUNT>
    </LEDGERENTRIES.LIST>
   </VOUCHER>
  </TALLYMESSAGE>
 </DATA></BODY>
</ENVELOPE>"""

EMPTY_XML = """<ENVELOPE>
 <HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER>
 <BODY><DESC><STATICVARIABLES><SVCURRENTCOMPANY>New New</SVCURRENTCOMPANY>
 </STATICVARIABLES></DESC><DATA>  </DATA></BODY>
</ENVELOPE>"""


def test_parse_company_list():
    assert parse_company_list(COMPANY_LIST_XML) == [
        "Acme Manufacturing Pvt Ltd",
        "New New",
    ]


def test_empty_company_returns_no_rows():
    """An empty Tally company answers STATUS 1 with a blank DATA block — success,
    not failure. The caller turns this into a useful message."""
    assert parse_purchase_vouchers(EMPTY_XML) == []


def test_malformed_xml_is_an_ingestion_error():
    with pytest.raises(IngestionError):
        parse_purchase_vouchers("<ENVELOPE><BODY>truncated")


def test_supplier_reference_is_the_invoice_number_not_our_voucher_number():
    """PV-CN-001 is our internal voucher; GSTR-2B only ever sees CN/INV/2026/801."""
    rows = parse_purchase_vouchers(VOUCHER_XML)
    assert rows[0].invoice_number == "CN/INV/2026/801"
    assert rows[0].raw["Voucher No."] == "PV-CN-001"


def test_reference_date_is_the_invoice_date_not_the_booking_date():
    """Booked 05 Aug, invoiced 28 Jul — 8 days apart, far outside the ±3 day tolerance."""
    assert parse_purchase_vouchers(VOUCHER_XML)[0].invoice_date == date(2026, 7, 28)


def test_igst_ledger_is_split_out_and_taxable_value_excludes_tax():
    row = parse_purchase_vouchers(VOUCHER_XML)[0]
    assert row.igst == Decimal("36000.00")
    assert row.cgst == Decimal("0")
    assert row.taxable_value == Decimal("200000.00")
    assert row.total_tax == Decimal("36000.00")


def test_cgst_sgst_ledgers_are_split_out():
    row = parse_purchase_vouchers(VOUCHER_XML)[1]
    assert row.cgst == Decimal("9000.00")
    assert row.sgst == Decimal("9000.00")
    assert row.igst == Decimal("0")
    assert row.total_tax == Decimal("18000.00")


def test_party_ledger_is_never_counted_as_taxable_value():
    """The party ledger carries the gross 236000; counting it would double the base."""
    assert parse_purchase_vouchers(VOUCHER_XML)[0].taxable_value == Decimal("200000.00")


def test_gstin_is_taken_when_tally_has_it():
    assert parse_purchase_vouchers(VOUCHER_XML)[1].gstin_normalized == "29AAACG4455S1Z7"


def test_missing_gstin_is_left_blank_for_name_resolution():
    """Tally usually keeps the GSTIN on the ledger master, not the voucher — the same
    gap a GSTIN-less CSV has, resolved the same way."""
    row = parse_purchase_vouchers(VOUCHER_XML)[0]
    assert row.gstin_normalized == ""
    assert row.vendor_name == "CloudNine Technologies Pvt Ltd"


def test_tally_rows_resolve_their_gstin_by_party_name():
    rows = parse_purchase_vouchers(VOUCHER_XML)
    resolved = resolve_missing_gstins(
        rows, [], {"CLOUDNINETECHNOLOGIES": "29AAACN5678P1Z3"}
    )
    assert resolved == 1
    assert rows[0].gstin_normalized == "29AAACN5678P1Z3"


def test_request_bounds_the_fetch_to_the_period():
    xml = build_purchase_register_request("Acme Ltd", "20260801", "20260831")
    assert "<SVCURRENTCOMPANY>Acme Ltd</SVCURRENTCOMPANY>" in xml
    assert '<SVFROMDATE TYPE="DATE">20260801</SVFROMDATE>' in xml
    assert "<VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>" in xml
