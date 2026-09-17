"""Demo GSTR-2B payloads in GSTN/GSP envelope shape.

`corrected` is the sandbox sample as-filed. `inconsistent` is the same sample
with seeded defects so matching has something to show (missing invoices, typos,
tax diffs, ITC blocked). Both go through `parse_gsp_gstr2b_response` unchanged.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Literal

from lockstep.core.exceptions import ValidationError

Gstr2bVariant = Literal["inconsistent", "corrected"]

_SAMPLE_RESPONSE_PATH = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "gsp_gstr2b_sample.json"
)

OMITTED_INVOICES = frozenset({"AIN2425002589265", "GINV-2024-10422"})
CLERICAL_INVOICES = {
    "ZOHO-INV-24112": "ZOHO-INV-2411Z",
    "IT/24-25/978": "IT/24-25/97B",
}
TAX_SCALE = {"AIN2425002587878": 1.03}
ITC_BLOCKED = {
    "RJI-2425-88341": "POS and supplier state are same but recipient state is different",
    "C24E242500023146": "ITC restricted under section 17(5)",
}


def parse_variant(raw: str | None) -> Gstr2bVariant:
    variant = (raw or "corrected").strip().lower()
    if variant not in ("inconsistent", "corrected"):
        raise ValidationError('variant must be "inconsistent" or "corrected"')
    return variant  # type: ignore[return-value]


def _load_sample() -> dict:
    return json.loads(_SAMPLE_RESPONSE_PATH.read_text())


def _b2b_vendors(payload: dict) -> list[dict]:
    return payload["data"]["data"]["data"]["docdata"]["b2b"]


def _round_money(value: float) -> float:
    return round(value, 2)


def _invoice_count(payload: dict) -> int:
    return sum(len(vendor.get("inv", [])) for vendor in _b2b_vendors(payload))


def _apply_inconsistent(payload: dict) -> dict:
    mutated = copy.deepcopy(payload)
    remaining_vendors: list[dict] = []
    for vendor in _b2b_vendors(mutated):
        kept: list[dict] = []
        for inv in vendor.get("inv", []):
            inum = str(inv.get("inum", ""))
            if inum in OMITTED_INVOICES:
                continue
            if inum in CLERICAL_INVOICES:
                inv["inum"] = CLERICAL_INVOICES[inum]
            scale = TAX_SCALE.get(inum)
            if scale is not None:
                inv["igst"] = _round_money(float(inv.get("igst") or 0) * scale)
                inv["cgst"] = _round_money(float(inv.get("cgst") or 0) * scale)
                inv["sgst"] = _round_money(float(inv.get("sgst") or 0) * scale)
                cess = float(inv.get("cess") or 0)
                inv["val"] = _round_money(
                    float(inv.get("txval") or 0)
                    + float(inv["igst"])
                    + float(inv["cgst"])
                    + float(inv["sgst"])
                    + cess
                )
            if inum in ITC_BLOCKED:
                inv["itcavl"] = "N"
                inv["rsn"] = ITC_BLOCKED[inum]
            kept.append(inv)
        if kept:
            vendor["inv"] = kept
            remaining_vendors.append(vendor)
    mutated["data"]["data"]["data"]["docdata"]["b2b"] = remaining_vendors
    return mutated


def defects_for(variant: Gstr2bVariant) -> dict[str, int]:
    sample_count = _invoice_count(_load_sample())
    if variant == "corrected":
        return {
            "exact": sample_count,
            "clerical": 0,
            "amount_mismatch": 0,
            "missing_in_2b": 0,
            "itc_ineligible": 0,
        }
    return {
        "exact": sample_count
        - len(OMITTED_INVOICES)
        - len(CLERICAL_INVOICES)
        - len(TAX_SCALE)
        - len(ITC_BLOCKED),
        "clerical": len(CLERICAL_INVOICES),
        "amount_mismatch": len(TAX_SCALE),
        "missing_in_2b": len(OMITTED_INVOICES),
        "itc_ineligible": len(ITC_BLOCKED),
    }


def get_gstr2b_payload(variant: Gstr2bVariant = "corrected") -> dict:
    sample = _load_sample()
    if variant == "corrected":
        return sample
    return _apply_inconsistent(sample)


def get_gstr2b_mock(variant: Gstr2bVariant = "corrected") -> dict:
    payload = get_gstr2b_payload(variant)
    return {
        "variant": variant,
        "count": _invoice_count(payload),
        "defects": defects_for(variant),
        "data": payload,
    }
