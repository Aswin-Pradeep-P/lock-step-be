"""CSV ingestion: turn an arbitrary Tally/Zoho/SAP/GSTR-2B export into
normalized rows, preserving every original column for later re-export.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

from lockstep.core.exceptions import IngestionError

# Canonical field -> header aliases we recognize (lowercased, whitespace-normalized).
# The purchase ledger and the GSTR-2B download use different header conventions;
# both are resolved against the same alias table.
CANONICAL_ALIASES: dict[str, list[str]] = {
    "invoice_number": [
        "invoice number", "invoice no", "invoice no.", "inv no", "invoice_no",
        "document number", "voucher no", "bill no", "invoice num",
    ],
    "gstin": [
        "gstin", "supplier gstin", "vendor gstin", "gstin of supplier",
        "gstin/uin of supplier", "supplier gstin/uin",
    ],
    "vendor_name": [
        "vendor name", "supplier name", "party name", "trade/legal name",
        "supplier trade name", "legal name of supplier",
    ],
    "taxable_value": [
        "taxable value", "invoice value", "total invoice value", "amount",
        "total amount", "value", "taxable amount",
    ],
    "invoice_date": [
        "invoice date", "date", "document date", "bill date",
    ],
    "integrated_tax": ["integrated tax", "igst", "integrated tax amount"],
    "central_tax": ["central tax", "cgst", "central tax amount"],
    "state_tax": ["state/ut tax", "sgst", "state tax", "state tax amount"],
    "cess": ["cess", "cess amount"],
    "description": ["description", "item description", "particulars", "hsn description"],
}

REQUIRED_CANONICAL_FIELDS = ("invoice_number", "gstin", "taxable_value")

_DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y", "%m/%d/%Y")
_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def _normalize_header(header: str) -> str:
    return re.sub(r"\s+", " ", header.strip().lower())


def resolve_columns(columns: list[str]) -> dict[str, str]:
    """Map canonical field name -> actual column name present in this CSV."""
    normalized = {_normalize_header(c): c for c in columns}
    resolved: dict[str, str] = {}
    for canonical, aliases in CANONICAL_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                resolved[canonical] = normalized[alias]
                break
        else:
            # substring fallback (e.g. "gstin of supplier (counter party)")
            for norm_header, original in normalized.items():
                if any(alias in norm_header for alias in aliases):
                    resolved[canonical] = original
                    break
    return resolved


def parse_csv(content: bytes) -> tuple[list[dict], dict[str, str]]:
    """Parse raw CSV bytes into (rows-as-dicts, canonical-column-map).

    Every value is read as a string (dtype=str) so raw_data round-trips
    exactly for CSV re-export; canonical parsing happens separately.
    """
    try:
        df = pd.read_csv(
            io.BytesIO(content), dtype=str, keep_default_na=False, encoding_errors="replace"
        )
    except Exception as exc:  # pandas raises many exception types on malformed CSV
        raise IngestionError(f"Could not parse CSV: {exc}") from exc

    if df.empty:
        raise IngestionError("CSV has no data rows")

    column_map = resolve_columns(list(df.columns))
    missing = [f for f in REQUIRED_CANONICAL_FIELDS if f not in column_map]
    if missing:
        raise IngestionError(
            f"Could not identify required column(s) {missing} in CSV. "
            f"Columns found: {list(df.columns)}"
        )

    rows = df.to_dict(orient="records")
    return rows, column_map


def normalize_gstin(gstin: str) -> str:
    return _NON_ALNUM.sub("", gstin.upper())


def normalize_invoice_number(invoice_number: str) -> str:
    return _NON_ALNUM.sub("", invoice_number.upper())


def parse_amount(raw: str) -> float:
    cleaned = re.sub(r"[^\d.\-]", "", raw or "")
    return float(cleaned) if cleaned not in ("", "-", ".") else 0.0


def parse_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    try:
        return pd.to_datetime(raw, dayfirst=True).date()
    except Exception:
        return None


@dataclass
class CanonicalRow:
    row_index: int
    invoice_number: str
    invoice_number_normalized: str
    gstin: str
    gstin_normalized: str
    vendor_name: str
    taxable_value: float
    itc_amount: float
    invoice_date: date | None
    description: str
    raw: dict = field(repr=False)


def to_canonical_rows(rows: list[dict], column_map: dict[str, str]) -> list[CanonicalRow]:
    canonical_rows = []
    for i, row in enumerate(rows):
        invoice_number = str(row.get(column_map.get("invoice_number", ""), "")).strip()
        gstin = str(row.get(column_map.get("gstin", ""), "")).strip()
        taxable_value = parse_amount(str(row.get(column_map.get("taxable_value", ""), "0")))
        tax_total = sum(
            parse_amount(str(row.get(column_map[field], "0")))
            for field in ("integrated_tax", "central_tax", "state_tax", "cess")
            if field in column_map
        )
        canonical_rows.append(
            CanonicalRow(
                row_index=i,
                invoice_number=invoice_number,
                invoice_number_normalized=normalize_invoice_number(invoice_number),
                gstin=gstin,
                gstin_normalized=normalize_gstin(gstin),
                vendor_name=str(row.get(column_map.get("vendor_name", ""), "")).strip(),
                taxable_value=taxable_value,
                itc_amount=tax_total if tax_total > 0 else taxable_value,
                invoice_date=parse_date(str(row.get(column_map.get("invoice_date", ""), ""))),
                description=str(row.get(column_map.get("description", ""), "")).strip(),
                raw=row,
            )
        )
    return canonical_rows
