"""Turn an arbitrary Tally / GSTR-2B export into normalized rows.

Header normalisation is a real task, not an afterthought: Tally column names vary
between versions and users, and the portal's 2B download carries a multi-row header.

Ingestion is deliberately source-shaped (`parse_file` -> rows -> `to_canonical_rows`)
so a GSTN API source can later produce the same `CanonicalRow` list without any
downstream change.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import pandas as pd

from lockstep.core.exceptions import IngestionError

# Canonical field -> header aliases we recognize (lowercased, whitespace-normalized).
CANONICAL_ALIASES: dict[str, list[str]] = {
    "invoice_number": [
        # The supplier's own document number first — that is what appears in GSTR-2B.
        "voucher ref. no.", "voucher ref no", "supplier invoice no", "ref. no.",
        "invoice number", "invoice no", "invoice no.", "inv no", "invoice_no",
        "document number", "bill no", "invoice num",
        # Our internal voucher number, only if nothing better exists.
        "voucher no", "voucher no.",
    ],
    "gstin": [
        "gstin", "supplier gstin", "vendor gstin", "gstin of supplier",
        "gstin/uin of supplier", "supplier gstin/uin",
    ],
    "vendor_name": [
        "vendor name", "supplier name", "party name", "trade/legal name",
        "supplier trade name", "legal name of supplier", "buyer/supplier",
    ],
    "taxable_value": [
        "taxable value", "taxable amount", "gross total", "invoice value",
        "total invoice value", "amount", "total amount", "value",
    ],
    "invoice_date": [
        # The supplier's invoice date, not our booking date.
        "voucher ref. date", "voucher ref date", "supplier invoice date",
        "invoice date", "document date", "bill date", "date",
    ],
    "integrated_tax": ["integrated tax", "igst", "igst input", "integrated tax amount"],
    "central_tax": ["central tax", "cgst", "cgst input", "central tax amount"],
    "state_tax": ["state/ut tax", "sgst", "sgst input", "state tax", "state tax amount"],
    "cess": ["cess", "cess amount"],
    # The most valuable column in the 2B file — when the supplier actually filed.
    # Never dropped: accumulated across periods it becomes the filing history.
    "filing_date": [
        "gstr-1/iff/gstr-5 filing date", "gstr-1/5 filing date", "filing date",
        "gstr1 filing date", "date of filing",
    ],
    "filing_period": [
        "gstr-1/iff/gstr-5 period", "gstr-1/5 period", "filing period", "return period",
    ],
    "itc_availability": ["itc availability", "itc available", "availability of itc"],
    "itc_reason": ["reason", "itc reason"],
    "reverse_charge": [
        "supply attract reverse charge", "reverse charge", "rcm", "is reverse charge",
    ],
    "invoice_type": ["invoice type", "document type", "voucher type"],
    "description": ["description", "item description", "hsn description", "narration"],
}

REQUIRED_CANONICAL_FIELDS = ("invoice_number", "taxable_value")

_DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y", "%m/%d/%Y")
_NON_ALNUM = re.compile(r"[^A-Z0-9]")
#: OCR-style character confusions seen in hand-keyed invoice numbers.
_CLERICAL_CONFUSIONS = str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5", "B": "8"})
_TRUEISH = {"y", "yes", "true", "1", "t"}
_FALSEISH = {"n", "no", "false", "0", "f"}
_MAX_HEADER_SCAN = 10  # portal 2B exports bury the real header a few rows down


def _normalize_header(header: str) -> str:
    return re.sub(r"\s+", " ", str(header).strip().lower())


def resolve_columns(columns: list[str]) -> dict[str, str]:
    """Map canonical field name -> actual column name present in this file."""
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


def _find_header_row(df: pd.DataFrame) -> int | None:
    """Locate the real header row in a multi-row-header export (portal GSTR-2B).

    Returns its positional index, or None if row 0 already looks like the header.
    """
    for i in range(min(_MAX_HEADER_SCAN, len(df))):
        candidate = [str(v) for v in df.iloc[i].tolist()]
        resolved = resolve_columns(candidate)
        if all(f in resolved for f in REQUIRED_CANONICAL_FIELDS) and len(resolved) >= 3:
            return i
    return None


def _frame_to_rows(df: pd.DataFrame) -> tuple[list[dict], dict[str, str]]:
    if df.empty:
        raise IngestionError("File has no data rows")

    column_map = resolve_columns(list(df.columns))
    if not all(f in column_map for f in REQUIRED_CANONICAL_FIELDS):
        header_row = _find_header_row(df)
        if header_row is not None:
            df = df.rename(columns=dict(zip(df.columns, df.iloc[header_row], strict=False)))
            df = df.iloc[header_row + 1:].reset_index(drop=True)
            column_map = resolve_columns(list(df.columns))

    missing = [f for f in REQUIRED_CANONICAL_FIELDS if f not in column_map]
    if missing:
        raise IngestionError(
            f"Could not identify required column(s) {missing}. "
            f"Columns found: {list(df.columns)[:25]}"
        )

    # Drop rows where the invoice number is blank — trailing total/notes rows.
    invoice_col = column_map["invoice_number"]
    df = df[df[invoice_col].astype(str).str.strip() != ""]
    if df.empty:
        raise IngestionError("File has no rows with an invoice number")

    return df.to_dict(orient="records"), column_map


def parse_csv(content: bytes) -> tuple[list[dict], dict[str, str]]:
    """Parse raw CSV bytes into (rows-as-dicts, canonical-column-map).

    Every value is read as a string so `raw_data` round-trips exactly for re-export;
    canonical parsing happens separately.
    """
    try:
        df = pd.read_csv(
            io.BytesIO(content), dtype=str, keep_default_na=False, encoding_errors="replace"
        )
    except Exception as exc:  # pandas raises many exception types on malformed CSV
        raise IngestionError(f"Could not parse CSV: {exc}") from exc
    return _frame_to_rows(df)


def parse_excel(content: bytes) -> tuple[list[dict], dict[str, str]]:
    try:
        df = pd.read_excel(io.BytesIO(content), dtype=str, keep_default_na=False)
    except Exception as exc:
        raise IngestionError(f"Could not parse Excel file: {exc}") from exc
    return _frame_to_rows(df)


def parse_file(filename: str, content: bytes) -> tuple[list[dict], dict[str, str]]:
    """Dispatch on extension. The one entry point ingestion sources should use."""
    if (filename or "").lower().endswith((".xlsx", ".xls")):
        return parse_excel(content)
    return parse_csv(content)


def normalize_gstin(gstin: str) -> str:
    return _NON_ALNUM.sub("", str(gstin).upper())


def normalize_invoice_number(invoice_number: str) -> str:
    """Case, spacing and punctuation only — the EXACT_MATCH key."""
    return _NON_ALNUM.sub("", str(invoice_number).upper())


def clerical_key(invoice_number: str) -> str:
    """Aggressive normalisation for the CLERICAL_MISMATCH tier.

    Folds the differences a human typing an invoice number actually makes: case,
    spacing, leading zeros, and O/0 I/1 L/1 S/5 B/8 confusions.
    """
    normalized = normalize_invoice_number(invoice_number).translate(_CLERICAL_CONFUSIONS)
    # Strip leading zeros from each digit run: INV007 and INV7 are the same invoice.
    return re.sub(r"0+(\d)", r"\1", normalized)


#: Dropped when matching a party name. Word-wise, so "CO" never eats the CO in
#: "COLD STORAGE" — the reason this is a word filter and not a regex.
_COMPANY_WORDS = frozenset(
    {"PVT", "PRIVATE", "LTD", "LIMITED", "LLP", "INC", "CORP", "CO", "COMPANY",
     "AND", "THE"}
)


def normalize_name(name: str) -> str:
    """Fold a party name for vendor lookup: case, punctuation, company suffixes.

    'CloudNine Technologies Pvt Ltd' and 'CLOUDNINE TECHNOLOGIES' resolve to the same
    vendor, which is what makes a GSTIN-less Tally export usable.
    """
    words = "".join(c if c.isalnum() else " " for c in str(name).upper()).split()
    return "".join(w for w in words if w not in _COMPANY_WORDS)


def parse_amount(raw: str) -> Decimal:
    """Money is Decimal end to end. Floats silently disagree with NUMERIC(14,2)."""
    cleaned = re.sub(r"[^\d.\-]", "", str(raw or ""))
    if cleaned in ("", "-", ".", "-."):
        return Decimal("0")
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0")


def parse_date(raw: str) -> date | None:
    raw = str(raw or "").strip()
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


def parse_bool(raw: str) -> bool | None:
    value = str(raw or "").strip().lower()
    if value in _TRUEISH:
        return True
    if value in _FALSEISH:
        return False
    return None


@dataclass
class CanonicalRow:
    row_index: int
    invoice_number: str
    invoice_number_normalized: str
    clerical_key: str
    gstin: str
    gstin_normalized: str
    vendor_name: str
    vendor_name_normalized: str
    taxable_value: Decimal
    igst: Decimal
    cgst: Decimal
    sgst: Decimal
    cess: Decimal
    invoice_date: date | None
    supplier_filed_at: date | None = None
    itc_available: bool | None = None
    itc_reason: str = ""
    is_reverse_charge: bool = False
    description: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def total_tax(self) -> Decimal:
        return self.igst + self.cgst + self.sgst + self.cess

    @property
    def itc_amount(self) -> Decimal:
        """Tax at stake. Falls back to taxable value when no tax columns were present."""
        return self.total_tax if self.total_tax > 0 else self.taxable_value


def to_canonical_rows(rows: list[dict], column_map: dict[str, str]) -> list[CanonicalRow]:
    def cell(row: dict, field_name: str) -> str:
        column = column_map.get(field_name)
        return str(row.get(column, "")).strip() if column else ""

    canonical_rows = []
    for i, row in enumerate(rows):
        invoice_number = cell(row, "invoice_number")
        gstin = cell(row, "gstin")
        canonical_rows.append(
            CanonicalRow(
                row_index=i,
                invoice_number=invoice_number,
                invoice_number_normalized=normalize_invoice_number(invoice_number),
                clerical_key=clerical_key(invoice_number),
                gstin=gstin,
                gstin_normalized=normalize_gstin(gstin),
                vendor_name=cell(row, "vendor_name"),
                vendor_name_normalized=normalize_name(cell(row, "vendor_name")),
                taxable_value=parse_amount(cell(row, "taxable_value")),
                igst=parse_amount(cell(row, "integrated_tax")),
                cgst=parse_amount(cell(row, "central_tax")),
                sgst=parse_amount(cell(row, "state_tax")),
                cess=parse_amount(cell(row, "cess")),
                invoice_date=parse_date(cell(row, "invoice_date")),
                supplier_filed_at=parse_date(cell(row, "filing_date")),
                itc_available=parse_bool(cell(row, "itc_availability")),
                itc_reason=cell(row, "itc_reason"),
                is_reverse_charge=parse_bool(cell(row, "reverse_charge")) or False,
                description=cell(row, "description"),
                raw=row,
            )
        )
    return canonical_rows
