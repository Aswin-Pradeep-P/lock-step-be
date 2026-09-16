"""Pure-function pieces of the reconciliation pipeline.

`run_check`/`_upsert_vendors` themselves need a live DB session and are verified by
API-level testing instead — the same boundary the rest of this suite already draws
around reconciliation.py, ai_summaries.py, actions.py and dashboard.py.
"""

from lockstep.models.vendor import Vendor
from lockstep.services.ingestion import (
    CanonicalRow,
    clerical_key,
    normalize_gstin,
    normalize_invoice_number,
    normalize_name,
)
from lockstep.services.reconciliation import VendorMap, _backfill_contact


def _row(invoice_number="INV-1", gstin="", vendor_name="Unknown Trader") -> CanonicalRow:
    return CanonicalRow(
        row_index=0,
        invoice_number=invoice_number,
        invoice_number_normalized=normalize_invoice_number(invoice_number),
        clerical_key=clerical_key(invoice_number),
        gstin=gstin,
        gstin_normalized=normalize_gstin(gstin) if gstin else "",
        vendor_name=vendor_name,
        vendor_name_normalized=normalize_name(vendor_name),
        taxable_value=0,
        igst=0,
        cgst=0,
        sgst=0,
        cess=0,
        invoice_date=None,
        raw={},
    )


def test_vendor_map_looks_up_by_gstin_first():
    verified = Vendor(gstin="29ABCDE1234F1Z5", name="Sharma Traders")
    vmap = VendorMap(by_gstin={"29ABCDE1234F1Z5": verified}, by_name={})
    assert vmap.get(_row(gstin="29ABCDE1234F1Z5")) is verified


def test_vendor_map_falls_back_to_name_when_no_gstin():
    orphan = Vendor(gstin=None, gstin_verified=False, name="Unknown Trader")
    vmap = VendorMap(by_gstin={}, by_name={normalize_name("Unknown Trader"): orphan})
    assert vmap.get(_row(gstin="", vendor_name="Unknown Trader")) is orphan


def test_vendor_map_returns_none_with_no_gstin_and_no_name():
    vmap = VendorMap(by_gstin={}, by_name={})
    assert vmap.get(_row(gstin="", vendor_name="")) is None


def test_backfill_contact_only_fills_blanks():
    vendor = Vendor(name="Sharma Traders", contact_email="existing@example.com")
    _backfill_contact(vendor, email="new@example.com", phone="9876543210")
    assert vendor.contact_email == "existing@example.com"  # not overwritten
    assert vendor.contact_phone == "9876543210"  # was blank, now filled


def test_backfill_contact_does_nothing_with_no_new_info():
    vendor = Vendor(name="Sharma Traders")
    _backfill_contact(vendor, email="", phone="")
    assert vendor.contact_email is None
    assert vendor.contact_phone is None
