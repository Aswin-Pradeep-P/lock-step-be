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
    # Not part of any government export — only ever present if a buyer's own
    # purchase register or vendor master carries it. When it is, it's the only way
    # a "nudge the vendor" reminder can ever have somewhere real to go.
    "vendor_email": [
        "vendor email", "supplier email", "contact email", "email", "email id",
        "email address",
    ],
    "vendor_phone": [
        "vendor phone", "supplier phone", "contact phone", "contact no", "contact number",
        "phone", "phone number", "mobile", "mobile number",
    ],
}

REQUIRED_CANONICAL_FIELDS = ("invoice_number", "taxable_value")

_DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y", "%m/%d/%Y")
_NON_ALNUM = re.compile(r"[^A-Z0-9]")
#: OCR-style character confusions seen in hand-keyed invoice numbers.
_CLERICAL_CONFUSIONS = str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5", "B": "8"})
_TRUEISH = {"y", "yes", "true", "1", "t"}
_FALSEISH = {"n", "no", "false", "0", "f"}
_MAX_HEADER_SCAN = 10  # portal 2B exports bury the real header a few rows down


#: A header often carries a trailing currency-unit annotation — "Taxable Value (₹)",
#: "Invoice Value(Rs.)" — that has nothing to do with which field it names. Stripping
#: it turns fragile substring guessing into a reliable exact match: without this,
#: "Taxable Value (₹)" and "Invoice Value(₹)" both fail an exact match on their own
#: canonical alias and fall through to substring search, where whichever column comes
#: first in the file wins — which is how "taxable_value" ended up resolving to the
#: wrong column on a real GSTR-2B export.
_CURRENCY_SUFFIX = re.compile(r"\(\s*(?:₹|rs\.?|inr)\s*\)\s*$")


def _normalize_header(header: str) -> str:
    normalized = re.sub(r"\s+", " ", str(header).strip().lower())
    return _CURRENCY_SUFFIX.sub("", normalized).strip()


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


def _merge_header_pair(parent: list, child: list) -> list:
    """Combine a spanning parent header row with its child sub-header row.

    The real GSTR-2B portal export has exactly this shape: 'Invoice Details' spans
    'Invoice number'/'Invoice type'/'Invoice Date'/'Invoice Value', and 'Tax Amount'
    spans 'Integrated Tax'/'Central Tax'/'State/UT Tax'/'Cess' — one row below. The
    child label is the specific, alias-matching one, so it wins whenever present;
    the parent label survives only for columns with no child ('GSTIN of supplier').
    """
    merged = []
    for idx, (p, c) in enumerate(zip(parent, child, strict=False)):
        c_str = "" if c is None else str(c).strip()
        if c_str.lower() == "nan":
            c_str = ""
        p_str = "" if p is None else str(p).strip()
        if p_str.lower() == "nan":
            p_str = ""
        merged.append(c_str or p_str or f"__col_{idx}__")
    return merged


def _find_header_row(df: pd.DataFrame) -> tuple[int, int] | None:
    """Locate the real header row(s) in a multi-row-header export (portal GSTR-2B).

    Returns `(start_row_index, rows_consumed)` — 2 rows for a parent/child merged
    header, otherwise 1 — or None if nothing in the scanned window looks like a
    header.
    """
    limit = min(_MAX_HEADER_SCAN, len(df))
    for i in range(limit):
        single = [str(v) for v in df.iloc[i].tolist()]
        resolved = resolve_columns(single)
        if all(f in resolved for f in REQUIRED_CANONICAL_FIELDS) and len(resolved) >= 3:
            return i, 1

        if i + 1 < limit:
            merged = _merge_header_pair(df.iloc[i].tolist(), df.iloc[i + 1].tolist())
            resolved = resolve_columns(merged)
            if all(f in resolved for f in REQUIRED_CANONICAL_FIELDS) and len(resolved) >= 3:
                return i, 2
    return None


def _frame_to_rows(df: pd.DataFrame) -> tuple[list[dict], dict[str, str]]:
    if df.empty:
        raise IngestionError("File has no data rows")

    column_map = resolve_columns(list(df.columns))
    if not all(f in column_map for f in REQUIRED_CANONICAL_FIELDS):
        located = _find_header_row(df)
        if located is not None:
            header_row, span = located
            if span == 2:
                new_columns = _merge_header_pair(
                    df.iloc[header_row].tolist(), df.iloc[header_row + 1].tolist()
                )
            else:
                new_columns = list(df.iloc[header_row])
            df = df.rename(columns=dict(zip(df.columns, new_columns, strict=False)))
            df = df.iloc[header_row + span:].reset_index(drop=True)
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


#: Keywords used to pick the right sheet out of a workbook that bundles more than
#: one (a real Tally export commonly ships an IGST ledger, a CGST/SGST ledger and a
#: GSTR-2B download as three sheets of one workbook — see the vendor-supplied
#: sample). Only used to disambiguate; a single-sheet workbook never needs this.
_SHEET_HINTS: dict[str, tuple[str, ...]] = {
    "ledger": ("tally", "igst", "cgst", "sgst", "purchase"),
    "gstr2b": ("gstr", "2b"),
}


def _pick_sheet(sheet_names: list[str], hint: str | None) -> str:
    keywords = _SHEET_HINTS.get(hint or "", ())
    for name in sheet_names:
        if any(k in name.lower() for k in keywords):
            return name
    return sheet_names[0]


def parse_excel(content: bytes, hint: str | None = None) -> tuple[list[dict], dict[str, str]]:
    try:
        workbook = pd.ExcelFile(io.BytesIO(content))
        sheet = _pick_sheet(workbook.sheet_names, hint)
        df = pd.read_excel(workbook, sheet_name=sheet, dtype=str, keep_default_na=False)
    except Exception as exc:
        raise IngestionError(f"Could not parse Excel file: {exc}") from exc
    return _frame_to_rows(df)


def parse_file(
    filename: str, content: bytes, hint: str | None = None
) -> tuple[list[dict], dict[str, str]]:
    """Dispatch on extension. The one entry point ingestion sources should use.

    `hint` is `"ledger"` or `"gstr2b"` — which side of the reconciliation this file
    is — used only to pick the right sheet out of a multi-sheet workbook.
    """
    if (filename or "").lower().endswith((".xlsx", ".xls")):
        return parse_excel(content, hint)
    return parse_csv(content)


#: A real GSTIN is always exactly 15 characters. A longer value is a data-entry error
#: or the wrong column mapped — never a GSTIN that happens to need truncating.
_GSTIN_LENGTH = 15


def normalize_gstin(gstin: str) -> str:
    """Empty (never guessed at) for anything that isn't a well-formed GSTIN.

    Storing an overlong value would crash the insert (the column is VARCHAR(15));
    silently truncating it would create a *different*, wrong-but-valid-looking GSTIN.
    Treating it as unresolved is the only safe choice — matching falls back to name.
    """
    normalized = _NON_ALNUM.sub("", str(gstin).upper())
    return normalized if len(normalized) == _GSTIN_LENGTH else ""


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
    vendor_email: str = ""
    vendor_phone: str = ""
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
                vendor_email=cell(row, "vendor_email"),
                vendor_phone=cell(row, "vendor_phone"),
                raw=row,
            )
        )
    return canonical_rows
