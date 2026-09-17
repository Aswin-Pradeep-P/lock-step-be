"""Generate the paired demo dataset: a purchase register and the GSTR-2B it reconciles
against, from one source of truth so the two halves cannot drift.

Run it:

    uv run python scripts/generate_demo_dataset.py           # regenerate + verify
    uv run python scripts/generate_demo_dataset.py --check   # fail if files are stale

Why a generator rather than hand-edited files: the 2B side is NOT derived from the
register at runtime (`StubGSPClient.fetch_gstr2b` discards the GSTIN and tax period it
is handed), so every invoice has to line up by construction. Hand-editing two files to
agree on 80 invoices is a losing game — the shipped 13-row pair already disagreed with
its own declared defect counts, because a clerical rewrite used a `2 -> Z` swap that
`clerical_key` does not fold, and nothing measured it.

The design, per period:

  * 80 register invoices, one match result each.
  * `inconsistent` (first fetch) omits 14 invoices  -> 66 in the 2B
  * `corrected`    (second fetch) omits only 5      -> 75 in the 2B

Nine suppliers file between the two fetches. That 9-invoice delta is the point of the
whole dataset: it is what the period view narrates as "9 filed since the last check",
and no one-shot reconciliation tool can show it.

Clerical, amount and ITC defects are identical across both variants — a typo and a
wrong tax amount do not fix themselves by waiting, and an ITC block is a portal ruling.
Only the filing status moves, which keeps the delta unambiguous.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = REPO_ROOT / "tests" / "fixtures"
FE_SAMPLES = REPO_ROOT.parent / "lock-step-fe" / "samples"

#: The period the demo opens on: its cutoff (13 Oct 2026) is still ahead, which is
#: the entire premise — there is still time to chase a supplier. A closed period
#: would demo a post-mortem.
SEED = 20260901
TAX_PERIOD = "092026"
BUYER_STATE = "29"  # Karnataka — matches seed.py's Acme Manufacturing (29AACCA1234M1Z7)
TARGET_TOTAL_TAX = Decimal("250000")

#: Bucket sizes. These are the numbers the demo narrates, so they are asserted, not hoped for.
N_EXACT = 50
N_MISSING = 14  # 9 recover in `corrected`, 5 stay missing
N_CLERICAL = 7
N_AMOUNT = 5
N_ITC = 4
N_RECOVERED = 9

PAISE = Decimal("0.01")


def money(value: Decimal | float | int) -> Decimal:
    return Decimal(str(value)).quantize(PAISE, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------------------
# Vendors
#
# A factory's register: raw material, components, consumables, packaging and job work,
# with a small tail of cloud/telecom/SaaS overheads. The overhead rows are the ones that
# already existed — four of them are pinned by assertions in tests/test_gsp.py, so they
# are reproduced to the paisa (see ANCHORS).
#
# State code drives the tax heads: 29 (Karnataka) is intrastate for this buyer and gets
# CGST+SGST; anything else is interstate and gets IGST.
# --------------------------------------------------------------------------------------


@dataclass
class Vendor:
    key: str
    name: str
    gstin: str
    category: str
    series: str
    count: int
    band: str
    e_invoice: bool = False
    filed_day: int = 8  # December day the GSTR-1 was filed; cutoff is the 13th

    @property
    def state(self) -> str:
        return self.gstin[:2]

    @property
    def intrastate(self) -> bool:
        return self.state == BUYER_STATE


VENDORS: list[Vendor] = [
    # --- raw material & metals -------------------------------------------------------
    Vendor("steelcraft", "Steelcraft Industries Ltd", "27AAACS9876K1Z1",
           "Raw material", "SCI/PO/{n:04d}", 3, "upper", True, 9),
    Vendor("bharat", "Bharat Heavy Works", "27AAACB1199H1Z3",
           "Castings", "BHW/SP/{n:04d}", 2, "upper", True, 11),
    Vendor("anand", "Anand Forgings Pvt Ltd", "24AABCA4471K1ZQ",
           "Forgings", "AF-2425-{n:04d}", 3, "mid", False, 12),
    Vendor("coromandel", "Coromandel Alloys Pvt Ltd", "33AAECC7781M1ZB",
           "Aluminium extrusion", "CAP/{n:04d}", 2, "mid", True, 10),
    Vendor("sundaram", "Sundaram Copper Works", "33AAFCS2290J1ZR",
           "Copper wire", "SCW-{n:04d}", 2, "mid", False, 7),
    Vendor("gujpoly", "Gujarat Polymers Ltd", "24AAACG8812N1ZD",
           "Polymer resin", "GPL/24-25/{n:04d}", 2, "mid", False, 12),
    # --- components ------------------------------------------------------------------
    Vendor("fasteners", "Precision Fasteners Pvt Ltd", "29AAGCP5512L1Z4",
           "Fasteners", "PF-{n:04d}", 3, "small", False, 6),
    Vendor("bearings", "Karnataka Bearings and Seals", "29AAJCK3390B1ZW",
           "Bearings", "KBS/{n:04d}", 3, "small", False, 8),
    Vendor("laxmi", "Laxmi Electricals and Trading Co", "29AAACL5544G1Z9",
           "Electricals", "LET/INV/{n:04d}", 2, "mid", False, 9),
    Vendor("digiworld", "DigiWorld Systems Pvt Ltd", "29AAACD2233W1Z6",
           "IT hardware", "DWS-24-{n:04d}", 2, "mid", True, 10),
    # --- consumables -----------------------------------------------------------------
    Vendor("apex", "Apex Chemicals Pvt Ltd", "36AAACA7766X1Z8",
           "Industrial chemicals", "APEX-{n:04d}", 2, "mid", False, 13),
    Vendor("gases", "Southern Industrial Gases Pvt Ltd", "29AAECS6634P1ZK",
           "Industrial gases", "SIG/{n:04d}", 4, "small", False, 7),
    Vendor("abrasives", "Hindustan Abrasives Ltd", "19AABCH9902R1ZT",
           "Abrasives", "HAL-{n:04d}", 2, "small", False, 11),
    Vendor("weldwell", "Weldwell Electrodes Pvt Ltd", "23AAFCW1123D1ZG",
           "Welding consumables", "WE/24/{n:04d}", 3, "small", False, 12),
    # --- packaging -------------------------------------------------------------------
    Vendor("greenpack", "GreenPack Solutions", "29AAACG4455S1Z7",
           "Packaging", "GPS-2024-{n:03d}", 3, "small", False, 6),
    Vendor("prestige", "Prestige Stationery House", "29AAABP1234M1Z8",
           "Stationery", "PSH-{n:04d}", 3, "small", False, 8),
    # --- job work & services ---------------------------------------------------------
    Vendor("deccan", "Deccan Heat Treatment Works", "29AAHCD8845Q1ZN",
           "Heat treatment", "DHT/{n:04d}", 3, "small", False, 10),
    Vendor("nandi", "Nandi Precision Machining", "29AAGCN2218F1ZY",
           "Machining job work", "NPM-{n:04d}", 4, "small", False, 9),
    Vendor("fasttrack", "FastTrack Cargo Services", "29AAACF8899T1Z2",
           "Freight", "FTC/BL/{n:04d}", 4, "small", False, 7),
    Vendor("sai", "Sai Analytical Labs", "29AALCS7712H1ZM",
           "Material testing", "SAL/RPT/{n:04d}", 2, "small", False, 12),
    Vendor("horizon", "Horizon Realty Partners", "29AAACH7788R1Z4",
           "Factory rent", "HRP/RENT/{n:03d}", 2, "large", False, 5),
    # --- ITC-blocked roles (see DEFECT_PLAN) -----------------------------------------
    Vendor("annapurna", "Annapurna Food Services", "29AAFCA3321V1ZE",
           "Canteen", "AFS/INV/{n:04d}", 2, "small", False, 9),
    Vendor("mumbaistay", "Mumbai Stay Suites Pvt Ltd", "27AAJCM5567T1ZP",
           "Hotel stay", "MSS/{n:04d}", 1, "mid", False, 10),
    Vendor("cityride", "CityRide Fleet Services", "29AAKCC8890L1ZX",
           "Employee cab", "CRF-{n:04d}", 1, "small", False, 8),
    Vendor("meridian", "Meridian Interiors LLP", "29AAEFM4432C1ZJ",
           "Works contract", "MI/24-25/{n:03d}", 1, "mid", False, 11),
    # --- overhead tail: cloud, telecom, SaaS -----------------------------------------
    Vendor("aws", "AMAZON WEB SERVICES INDIA PRIVATE LIMITED", "07AAJCA9880A1ZL",
           "Cloud hosting", "AIN24250025{n:05d}", 3, "upper", True, 10),
    Vendor("google", "GOOGLE INDIA PVT LTD", "06AACCG0527D1Z8",
           "Advertising", "GINV-2024-{n:05d}", 2, "small", True, 9),
    Vendor("jio", "JIO PLATFORMS LIMITED", "29AAECJ6878N1ZU",
           "Telecom", "C24E2425000{n:05d}", 2, "small", True, 8),
    Vendor("rjio", "Reliance Jio Infocomm Limited", "29AABCI6363G1ZP",
           "Telecom", "RJI-2425-{n:05d}", 2, "small", True, 8),
    Vendor("zoho", "ZOHO CORPORATION PRIVATE LIMITED", "33AAACZ4322M2Z9",
           "SaaS", "ZOHO-INV-{n:05d}", 2, "small", True, 7),
    Vendor("itech", "I TECH STORE", "29AACFI9070L1Z5",
           "IT hardware", "IT/24-25/{n:03d}", 2, "small", True, 9),
    Vendor("digitap", "DIGITAP.AI ENTERPRISE SOLUTIONS PRIVATE LIMITED", "29AAHCD5090M1Z3",
           "Software", "2024-25/DIG{n:05d}", 2, "mid", False, 11),
    Vendor("greyswift", "Grey Swift Private Limited", "06AAGCG5872M1Z3",
           "Software", "LE/25/1/{n:05d}", 2, "small", False, 10),
    Vendor("gcloud", "Google Cloud India Private Limited", "27AAGCG4576J1Z6",
           "Cloud hosting", "GCP-{n:05d}", 1, "small", True, 9),
    Vendor("gourmesserie", "GOURMESSERIE LLP", "29AAWFG9936H1ZP",
           "Office catering", "PRN-{n:04d}", 1, "small", False, 12),
]

#: Taxable-value bands, in rupees. The mix has to average ~Rs 17k taxable to land
#: ~Rs 2.5L of tax over 80 invoices, while still looking like a real factory ledger:
#: mostly small consumables with a short tail of raw-material and rent invoices.
BANDS = {
    "small": (2_000, 22_000),
    "mid": (22_000, 70_000),
    "upper": (70_000, 180_000),
    "large": (180_000, 260_000),
}

#: GST rate by category. Freight at 5%, job work and printing at 12%, cab at 28%.
RATES = {
    "Freight": Decimal("0.05"),
    "Machining job work": Decimal("0.12"),
    "Heat treatment": Decimal("0.12"),
    "Material testing": Decimal("0.18"),
    "Stationery": Decimal("0.12"),
    "Employee cab": Decimal("0.28"),
    "Canteen": Decimal("0.05"),
    "Office catering": Decimal("0.05"),
}
DEFAULT_RATE = Decimal("0.18")

#: Invoices reproduced to the paisa because tests/test_gsp.py asserts on them.
#: Excluded from the tax-fitting scale so regeneration cannot move them.
ANCHORS: dict[str, dict] = {
    "AIN2425002587878": {
        "vendor": "aws", "txval": Decimal("69229.92"), "rate": Decimal("0.18"),
        "invoice_date": date(2026, 9, 2), "narration": "AWS September usage",
    },
    "C24E242500023146": {
        "vendor": "jio", "txval": Decimal("4801.20"), "rate": Decimal("0.18"),
        "invoice_date": date(2026, 9, 5), "narration": "Jio Platforms",
    },
    "IT/24-25/978": {
        "vendor": "itech", "txval": Decimal("2203.38"), "rate": Decimal("0.18"),
        "invoice_date": date(2026, 9, 8), "narration": "IT equipment",
    },
    "ZOHO-INV-24112": {
        "vendor": "zoho", "txval": Decimal("1600.00"), "rate": Decimal("0.18"),
        "invoice_date": date(2026, 9, 1), "narration": "Zoho subscription",
    },
}


@dataclass
class Invoice:
    vendor: Vendor
    number: str
    invoice_date: date
    booking_date: date
    taxable: Decimal
    rate: Decimal
    narration: str
    voucher_no: str
    anchor: bool = False
    # assigned later
    defect: str = "exact"
    two_b_number: str | None = None
    two_b_tax_delta: Decimal = Decimal("0")
    itc_reason: str | None = None
    recovers: bool = False
    irn: str = ""

    @property
    def igst(self) -> Decimal:
        return Decimal("0") if self.vendor.intrastate else money(self.taxable * self.rate)

    @property
    def cgst(self) -> Decimal:
        return money(self.taxable * self.rate / 2) if self.vendor.intrastate else Decimal("0")

    @property
    def sgst(self) -> Decimal:
        return self.cgst

    @property
    def total_tax(self) -> Decimal:
        return self.igst + self.cgst + self.sgst

    @property
    def gross(self) -> Decimal:
        return money(self.taxable + self.total_tax)


# --------------------------------------------------------------------------------------
# Defect plan — assigned by explicit vendor/slot, never randomly, so the demo narrative
# is readable in one place and reproducible across runs.
# --------------------------------------------------------------------------------------

#: (vendor key, slot, rewrite) -> the 2B spelling. Only folds `clerical_key` actually
#: performs: O<->0, I<->1, L<->1, S<->5, B<->8, and leading-zero runs. The shipped
#: dataset used a `2 -> Z` swap, which does NOT fold, so that row silently became a
#: missing invoice on both sides while the declared counts kept claiming a clerical.
#: Every pair here is asserted at generation time against the real `clerical_key`.
#: Vendors whose invoice number is mis-keyed on the portal side. The rewrite is chosen
#: per number by `_mis_key`, because a fixed substitution ("swap the 5") silently does
#: nothing when the generated number has no 5 — and a rewrite that does nothing produces
#: an EXACT match while the defect plan still claims a clerical.
CLERICAL_SLOTS = [
    ("laxmi", 0), ("prestige", 0), ("greenpack", 0), ("fasteners", 0),
    ("abrasives", 0), ("bearings", 1), ("weldwell", 1),
]

#: Digit -> letter substitutions that `clerical_key` actually folds back. Applied to the
#: 2B side only: someone at the supplier's end read a 0 as an O.
_MIS_KEY_FOLDS = (("0", "O"), ("1", "I"), ("5", "S"), ("8", "B"))


def _mis_key(number: str) -> str:
    """Mis-type one character of `number` the way a human actually does.

    Substitutes the LAST occurrence, not the first: a leading-zero run is collapsed by
    `clerical_key`, so mangling a character inside one can fold two different numbers
    together.
    """
    for digit, letter in _MIS_KEY_FOLDS:
        index = number.rfind(digit)
        if index > 0:
            return number[:index] + letter + number[index + 1:]
    raise SystemExit(f"no foldable digit in {number!r} to mis-key")

#: (vendor key, slot, rupee delta) applied to the 2B tax. Always >= Rs 50 so it can
#: never be mistaken for rounding, and never combined with an ITC block: tier 3 does not
#: run `_classify_matched_pair`, so a blocked amount-mismatch row would silently stay
#: AMOUNT_MISMATCH and the ITC count would drift.
AMOUNT_SLOTS = [
    ("steelcraft", 0, Decimal("612.00")),
    ("gujpoly", 0, Decimal("-248.50")),   # 2B lower than the books — the scarier direction
    ("apex", 0, Decimal("175.00")),
    ("coromandel", 0, Decimal("-96.00")),
    ("nandi", 0, Decimal("100.00")),      # a flat transposition
]

#: (vendor key, slot, reason). Exact key, exact tax, exact date — they match cleanly at
#: tier 1 and `_classify_matched_pair` then reclassifies them, which is the only way an
#: invoice that IS in the 2B still cannot be claimed.
ITC_SLOTS = [
    ("annapurna", 0, "ITC restricted under section 17(5)"),
    ("cityride", 0, "ITC restricted under section 17(5)"),
    ("mumbaistay", 0, "POS and supplier state are same but recipient state is different"),
    ("meridian", 0, "ITC restricted under section 17(5)"),
]

#: Suppliers who had not filed at the first fetch but file correctly before the second.
#: Small, routine spend — the ones a chase call actually works on, ~Rs 16k of tax total.
RECOVERING_SLOTS = [
    # Two vendors go COMPLETELY dark at the first fetch and file in full before the
    # second. A vendor who is wholly absent is what `vendors_not_filed` counts and what
    # the vendor-risk panel ranks — scattering single misses across vendors who did file
    # leaves that headline reading "0 vendors haven't filed" next to a Rs 61k exposure.
    ("gases", 0), ("gases", 1), ("gases", 2), ("gases", 3),
    ("fasttrack", 0), ("fasttrack", 1), ("fasttrack", 2), ("fasttrack", 3),
    ("deccan", 0),
]

#: Chronic non-filers — absent from BOTH variants, and deliberately the larger invoices.
#: These are what the vendor-risk panel should rank at the top after the second fetch.
CHRONIC_SLOTS = [
    # Sundaram never files at all — both its invoices stay missing through both
    # fetches, so it is still on the risk list after the recovery.
    ("sundaram", 0), ("sundaram", 1),
    ("anand", 0), ("bharat", 0), ("horizon", 0),
]

ITC_NARRATIONS = {
    "annapurna": "Monthly canteen contract - outdoor catering",
    "cityride": "Rent-a-cab for client visit",
    "mumbaistay": "Hotel stay - Mumbai site visit",
    "meridian": "Office civil works - works contract",
}


def _rate_for(vendor: Vendor) -> Decimal:
    return RATES.get(vendor.category, DEFAULT_RATE)


def build_invoices(rng: random.Random) -> list[Invoice]:
    """One pass over the vendor table, emitting each vendor's invoices in slot order."""
    invoices: list[Invoice] = []
    anchor_by_vendor: dict[str, list[tuple[str, dict]]] = {}
    for number, spec in ANCHORS.items():
        anchor_by_vendor.setdefault(spec["vendor"], []).append((number, spec))

    seq = 1000
    for vendor in VENDORS:
        anchors = list(anchor_by_vendor.get(vendor.key, []))
        for slot in range(vendor.count):
            seq += 1
            if anchors:
                number, spec = anchors.pop(0)
                invoice_date = spec["invoice_date"]
                taxable, rate = spec["txval"], spec["rate"]
                narration = spec["narration"]
                is_anchor = True
            else:
                number = vendor.series.format(n=seq)
                invoice_date = date(2026, 9, rng.randint(1, 28))
                low, high = BANDS[vendor.band]
                taxable = money(rng.randint(low, high))
                rate = _rate_for(vendor)
                narration = f"{vendor.category} - September"
                is_anchor = False

            invoices.append(
                Invoice(
                    vendor=vendor,
                    number=number,
                    invoice_date=invoice_date,
                    # Booked a few days after the supplier's invoice date. This is why
                    # `Voucher Ref. Date` exists and must beat `Date` in the alias table.
                    booking_date=invoice_date + timedelta(days=rng.randint(0, 4)),
                    taxable=taxable,
                    rate=rate,
                    narration=narration,
                    # Full vendor key, not a 4-char prefix: "fasteners" and
                    # "fasttrack" both truncate to FAST, and the Tally loader keys
                    # its REMOTEID on this, so a collision silently overwrites a
                    # voucher instead of creating one.
                    voucher_no=f"PV-{vendor.key.upper()}-{slot + 1:02d}",
                    anchor=is_anchor,
                )
            )
    return invoices


def _slot_index(invoices: list[Invoice], vendor_key: str, slot: int) -> int:
    matches = [i for i, inv in enumerate(invoices) if inv.vendor.key == vendor_key]
    if slot >= len(matches):
        raise SystemExit(f"vendor {vendor_key} has no slot {slot} (has {len(matches)})")
    return matches[slot]


def assign_defects(invoices: list[Invoice]) -> None:
    from lockstep.services.ingestion import clerical_key, normalize_invoice_number

    taken: set[int] = set()

    def claim(vendor_key: str, slot: int, label: str) -> Invoice:
        idx = _slot_index(invoices, vendor_key, slot)
        if idx in taken:
            raise SystemExit(f"{label}: {vendor_key}[{slot}] already carries a defect")
        taken.add(idx)
        return invoices[idx]

    for vendor_key, slot in CLERICAL_SLOTS:
        inv = claim(vendor_key, slot, "clerical")
        inv.defect = "clerical"
        inv.two_b_number = _mis_key(inv.number)
        if normalize_invoice_number(inv.two_b_number) == normalize_invoice_number(inv.number):
            raise SystemExit(f"clerical {inv.number}: 2B spelling normalises identically")
        if clerical_key(inv.two_b_number) != clerical_key(inv.number):
            raise SystemExit(
                f"clerical {inv.number} -> {inv.two_b_number}: clerical_key does not fold "
                f"({clerical_key(inv.number)} vs {clerical_key(inv.two_b_number)})"
            )

    for vendor_key, slot, delta in AMOUNT_SLOTS:
        inv = claim(vendor_key, slot, "amount")
        inv.defect = "amount"
        inv.two_b_tax_delta = delta

    for vendor_key, slot, reason in ITC_SLOTS:
        inv = claim(vendor_key, slot, "itc")
        inv.defect = "itc"
        inv.itc_reason = reason
        inv.narration = ITC_NARRATIONS.get(vendor_key, inv.narration)

    for vendor_key, slot in RECOVERING_SLOTS:
        inv = claim(vendor_key, slot, "missing")
        inv.defect, inv.recovers = "missing", True

    for vendor_key, slot in CHRONIC_SLOTS:
        inv = claim(vendor_key, slot, "missing")
        inv.defect, inv.recovers = "missing", False

    # One missing row carries a Sec 17(5) keyword so risk_rules.sec17_5_hint fires and
    # the advisory sentence appears in the demo. It changes no status.
    for inv in invoices:
        if inv.defect == "missing" and inv.recovers:
            inv.narration = "Canteen gas cylinders - staff canteen"
            break


def fit_total_tax(invoices: list[Invoice]) -> None:
    """Scale the non-anchor taxable values once so total tax lands on the target.

    Deterministic by construction: draw, scale, round, assert. No search loop. Anchors
    are excluded because tests assert their exact amounts.
    """
    free = [i for i in invoices if not i.anchor]
    pinned_tax = sum((i.total_tax for i in invoices if i.anchor), Decimal("0"))
    free_tax = sum((i.total_tax for i in free), Decimal("0"))
    if free_tax <= 0:
        raise SystemExit("no scalable tax")

    k = (TARGET_TOTAL_TAX - pinned_tax) / free_tax
    for inv in free:
        scaled = inv.taxable * k
        # Round to the nearest Rs 10 so the amounts read like real invoices.
        inv.taxable = money((scaled / 10).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * 10)
        if inv.taxable < 100:
            inv.taxable = Decimal("100.00")

    total = sum((i.total_tax for i in invoices), Decimal("0"))
    if not (Decimal("200000") <= total <= Decimal("300000")):
        raise SystemExit(f"total tax {total} outside the Rs 2-3L band")


def rebalance_missing(invoices: list[Invoice]) -> None:
    """Push the missing invoices onto their target exposure figures.

    The demo narrates two numbers — roughly Rs 16k recovered between fetches and
    Rs 25k still stuck afterwards — so they are set deliberately rather than left to
    whatever the band happened to draw.
    """
    recovering = [i for i in invoices if i.defect == "missing" and i.recovers]
    chronic = [i for i in invoices if i.defect == "missing" and not i.recovers]

    def retarget(rows: list[Invoice], target_tax: Decimal) -> None:
        per_row = target_tax / len(rows)
        for inv in rows:
            taxable = per_row / inv.rate
            inv.taxable = money((taxable / 10).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * 10)

    retarget(recovering, Decimal("16000"))
    retarget(chronic, Decimal("25000"))


# --------------------------------------------------------------------------------------
# Emitters
# --------------------------------------------------------------------------------------

REGISTER_HEADER = [
    "Date", "Particulars", "Buyer/Supplier", "GSTIN", "Voucher Type", "Voucher No.",
    "Voucher Ref. No.", "Voucher Ref. Date", "Narration", "Gross Total",
    "IGST Input", "CGST Input", "SGST Input",
]


def write_register_csv(invoices: list[Invoice], path: Path) -> None:
    """The ledger side, in Tally purchase-register shape.

    `Gross Total` is deliberately gross (taxable + tax), not the taxable value: that is
    what a Tally export carries, `scripts/load_tally_demo_data.py` subtracts the tax
    back out of it, and the matcher never compares taxable value anyway.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(REGISTER_HEADER)
        for inv in invoices:
            writer.writerow([
                inv.booking_date.isoformat(),
                inv.vendor.category,
                inv.vendor.name,
                inv.vendor.gstin,
                "Purchase",
                inv.voucher_no,
                inv.number,
                inv.invoice_date.isoformat(),
                inv.narration,
                f"{inv.gross:.2f}",
                f"{inv.igst:.2f}",
                f"{inv.cgst:.2f}",
                f"{inv.sgst:.2f}",
            ])


def _two_b_invoice(inv: Invoice) -> dict:
    """One invoice as GSTN's `inv[]` entry, with this row's defect already applied."""
    igst, cgst, sgst = inv.igst, inv.cgst, inv.sgst
    if inv.two_b_tax_delta:
        if inv.vendor.intrastate:
            half = money(inv.two_b_tax_delta / 2)
            cgst, sgst = money(cgst + half), money(sgst + half)
        else:
            igst = money(igst + inv.two_b_tax_delta)

    total_tax = money(igst + cgst + sgst)
    entry = {
        "inum": inv.two_b_number or inv.number,
        "dt": inv.invoice_date.strftime("%d-%m-%Y"),
        "val": float(money(inv.taxable + total_tax)),
        "txval": float(inv.taxable),
        "igst": float(igst),
        "cgst": float(cgst),
        "sgst": float(sgst),
        "cess": 0.0,
        "pos": inv.vendor.state if inv.defect == "itc" and inv.vendor.key == "mumbaistay"
        else BUYER_STATE,
        "rev": "N",
        "typ": "R",
        "itcavl": "N" if inv.defect == "itc" else "Y",
        "rsn": inv.itc_reason or "",
        "imsStatus": "N",
    }
    if inv.irn:
        entry["irn"] = inv.irn
        entry["irngendate"] = inv.invoice_date.strftime("%d-%m-%Y")
        entry["srctyp"] = "e-Invoice"
    return entry


def _filed_on(vendor: Vendor, late: bool) -> str:
    """GSTR-1 filing date. Period 092026's cutoff is 13-10-2026, so a date after it is
    exactly what makes a supplier late — and what the filing history bands read."""
    day = vendor.filed_day if not late else 16
    return date(2026, 10, day).strftime("%d-%m-%Y")


def build_payload(invoices: list[Invoice], variant: str) -> dict:
    """Assemble the GSTN envelope for one variant.

    `inconsistent` omits every missing invoice (14). `corrected` omits only the chronic
    five — the nine that recover are present, correct, and filed after the 13th, which
    is the whole point: they filed late, and only a chase got them there.
    """
    omitted = {
        id(i) for i in invoices
        if i.defect == "missing" and (variant == "inconsistent" or not i.recovers)
    }

    vendors_out: list[dict] = []
    for vendor in VENDORS:
        rows = [i for i in invoices if i.vendor.key == vendor.key and id(i) not in omitted]
        if not rows:
            continue
        recovered_here = any(
            i.defect == "missing" and i.recovers for i in invoices
            if i.vendor.key == vendor.key
        )
        entries = [_two_b_invoice(i) for i in rows]
        vendors_out.append({
            "ctin": vendor.gstin,
            "trdnm": vendor.name,
            "supprd": TAX_PERIOD,
            "supfildt": _filed_on(vendor, late=variant == "corrected" and recovered_here),
            "inv": entries,
        })

    docdata = {"b2b": vendors_out}
    cpsumm = {"b2b": [_vendor_summary(v) for v in vendors_out]}
    inner = {"cpsumm": cpsumm, "docdata": docdata}
    serialised = json.dumps(docdata, sort_keys=True).encode()
    return {
        "code": 200,
        "transaction_id": f"lockstep-demo-{variant}",
        "data": {
            "status_cd": "1",
            "data": {
                "chksum": hashlib.sha256(serialised).hexdigest(),
                "data": inner,
            },
        },
        "timestamp": "2026-10-14T06:00:00Z",
    }


def _vendor_summary(vendor_out: dict) -> dict:
    """cpsumm roll-up. The parser ignores it, but a fixture whose summary contradicts
    its own detail reads as broken the moment anyone opens the raw JSON."""
    rows = vendor_out["inv"]
    return {
        "ctin": vendor_out["ctin"],
        "trdnm": vendor_out["trdnm"],
        "supprd": vendor_out["supprd"],
        "ttldocs": len(rows),
        "txval": round(sum(r["txval"] for r in rows), 2),
        "igst": round(sum(r["igst"] for r in rows), 2),
        "cgst": round(sum(r["cgst"] for r in rows), 2),
        "sgst": round(sum(r["sgst"] for r in rows), 2),
        "cess": 0.0,
    }


def assign_irns(invoices: list[Invoice]) -> None:
    """Deterministic 64-hex IRNs for the e-invoiced vendors, so the field is populated
    and stable across regenerations."""
    for inv in invoices:
        if inv.vendor.e_invoice:
            inv.irn = hashlib.sha256(f"lockstep:{inv.number}".encode()).hexdigest()


def write_json(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------------------
# Verification — re-read from disk and run the real matcher
# --------------------------------------------------------------------------------------

EXPECTED = {
    "inconsistent": {
        "EXACT_MATCH": N_EXACT, "MISSING_IN_GSTR2B": N_MISSING,
        "CLERICAL_MISMATCH": N_CLERICAL, "AMOUNT_MISMATCH": N_AMOUNT,
        "ITC_INELIGIBLE": N_ITC,
    },
    "corrected": {
        "EXACT_MATCH": N_EXACT + N_RECOVERED,
        "MISSING_IN_GSTR2B": N_MISSING - N_RECOVERED,
        "CLERICAL_MISMATCH": N_CLERICAL, "AMOUNT_MISMATCH": N_AMOUNT,
        "ITC_INELIGIBLE": N_ITC,
    },
}


def reconcile(register_path: Path, payload: dict) -> dict[str, int]:
    from collections import Counter

    from lockstep.services.gsp import parse_gsp_gstr2b_response
    from lockstep.services.ingestion import parse_file, to_canonical_rows
    from lockstep.services.matching import match_invoices, resolve_missing_gstins

    rows, mapping = parse_file(register_path.name, register_path.read_bytes(), hint="ledger")
    ledger = to_canonical_rows(rows, mapping)
    two_b = parse_gsp_gstr2b_response(payload)
    resolve_missing_gstins(ledger, two_b)
    results = match_invoices(ledger, two_b)
    return dict(Counter(str(r.status) for r in results))


def verify(register_path: Path, payloads: dict[str, dict]) -> bool:
    ok = True
    for variant, payload in payloads.items():
        counts = reconcile(register_path, payload)
        expected = EXPECTED[variant]
        matched = {k: counts.get(k, 0) for k in expected}
        extra = {k: v for k, v in counts.items() if k not in expected}
        status = "OK " if matched == expected and not extra else "FAIL"
        if status == "FAIL":
            ok = False
        print(f"  [{status}] {variant:13} {matched}" + (f" + unexpected {extra}" if extra else ""))
        if status == "FAIL":
            print(f"         expected {expected}")
    return ok


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------

FIXTURE_JSON = FIXTURES / "gsp_gstr2b_sample.json"
CORRECTED_JSON = FIXTURES / "gsp_gstr2b_corrected.json"
FIXTURE_CSV = FIXTURES / "purchase_register_gsp.csv"
FE_CSV = FE_SAMPLES / "purchase_register_gsp.csv"


def build() -> tuple[list[Invoice], dict[str, dict]]:
    rng = random.Random(SEED)
    invoices = build_invoices(rng)
    if len(invoices) != N_EXACT + N_MISSING + N_CLERICAL + N_AMOUNT + N_ITC:
        raise SystemExit(
            f"vendor table yields {len(invoices)} invoices, expected "
            f"{N_EXACT + N_MISSING + N_CLERICAL + N_AMOUNT + N_ITC} — adjust Vendor.count"
        )
    assign_defects(invoices)
    fit_total_tax(invoices)
    rebalance_missing(invoices)
    assign_irns(invoices)
    assert_unique_keys(invoices)
    payloads = {
        "inconsistent": build_payload(invoices, "inconsistent"),
        "corrected": build_payload(invoices, "corrected"),
    }
    return invoices, payloads


def assert_unique_keys(invoices: list[Invoice]) -> None:
    """No two invoices may collide on (GSTIN, normalised number) or the DUPLICATE tier
    fires — it is checked before any matching and silently eats the row. Also guard the
    clerical key, where a collision would let one row steal another's 2B match."""
    from lockstep.services.ingestion import clerical_key, normalize_invoice_number

    seen: set[tuple[str, str]] = set()
    folded: set[tuple[str, str]] = set()
    for inv in invoices:
        key = (inv.vendor.gstin, normalize_invoice_number(inv.number))
        if key in seen:
            raise SystemExit(f"duplicate invoice key {key} — would fire DUPLICATE")
        seen.add(key)
        fold = (inv.vendor.gstin, clerical_key(inv.number))
        if fold in folded:
            raise SystemExit(f"clerical-key collision {fold} — rows could steal each other")
        folded.add(fold)
        if inv.two_b_number:
            fold2 = (inv.vendor.gstin, clerical_key(inv.two_b_number))
            if fold2 in folded and fold2 != fold:
                raise SystemExit(f"2B clerical-key collision {fold2}")


def report(invoices: list[Invoice]) -> None:
    total_tax = sum((i.total_tax for i in invoices), Decimal("0"))
    recovering = [i for i in invoices if i.defect == "missing" and i.recovers]
    chronic = [i for i in invoices if i.defect == "missing" and not i.recovers]
    rec_tax = sum((i.total_tax for i in recovering), Decimal("0"))
    chr_tax = sum((i.total_tax for i in chronic), Decimal("0"))
    intrastate = sum(1 for i in invoices if i.vendor.intrastate)

    vendors = len({i.vendor.key for i in invoices})
    print(f"  invoices        {len(invoices)}  across {vendors} vendors")
    print(f"  total tax       Rs {total_tax:,.2f}")
    missing = len(recovering) + len(chronic)
    print(f"  at risk (1st)   Rs {rec_tax + chr_tax:,.2f}   ({missing} invoices)")
    print(f"  recovered       Rs {rec_tax:,.2f}   ({len(recovering)} file before the 2nd fetch)")
    print(f"  still at risk   Rs {chr_tax:,.2f}   ({len(chronic)} chronic non-filers)")
    interstate = len(invoices) - intrastate
    print(f"  tax heads       {intrastate} intrastate / {interstate} interstate")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="regenerate in memory and fail if the files on disk differ")
    parser.add_argument("--no-verify", action="store_true")
    args = parser.parse_args()

    invoices, payloads = build()

    if args.check:
        import io
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(REGISTER_HEADER)
        stale = []
        if FIXTURE_JSON.exists():
            if json.loads(FIXTURE_JSON.read_text()) != payloads["inconsistent"]:
                stale.append(str(FIXTURE_JSON))
        else:
            stale.append(str(FIXTURE_JSON))
        if stale:
            print("stale, regenerate:", *stale, sep="\n  ")
            return 1
        print("up to date")
        return 0

    write_register_csv(invoices, FIXTURE_CSV)
    write_json(payloads["inconsistent"], FIXTURE_JSON)
    write_json(payloads["corrected"], CORRECTED_JSON)
    if FE_SAMPLES.exists():
        write_register_csv(invoices, FE_CSV)

    print("wrote:")
    for path in (FIXTURE_CSV, FIXTURE_JSON, CORRECTED_JSON):
        print(f"  {path.relative_to(REPO_ROOT)}")
    if FE_SAMPLES.exists():
        print(f"  {FE_CSV}")
    print()
    report(invoices)
    print()

    if args.no_verify:
        return 0
    print("reconciling against the real matcher:")
    return 0 if verify(FIXTURE_CSV, payloads) else 1


if __name__ == "__main__":
    sys.exit(main())
