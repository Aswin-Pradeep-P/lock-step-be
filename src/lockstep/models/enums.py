import enum


class InvoiceMatchStatus(enum.StrEnum):
    """Matching-engine outcome for one invoice. Assigned by rules only — never by the LLM."""

    PENDING = "PENDING"
    EXACT_MATCH = "EXACT_MATCH"
    CLERICAL_MISMATCH = "CLERICAL_MISMATCH"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    MISSING_IN_GSTR2B = "MISSING_IN_GSTR2B"  # supplier hasn't filed — the actionable one
    MISSING_IN_LEDGER = "MISSING_IN_LEDGER"
    DUPLICATE = "DUPLICATE"
    ITC_INELIGIBLE = "ITC_INELIGIBLE"
    RESOLVED = "RESOLVED"
    CARRIED_FORWARD = "CARRIED_FORWARD"


#: Statuses that represent tax we may not be able to claim this period.
AT_RISK_STATUSES = (
    InvoiceMatchStatus.MISSING_IN_GSTR2B,
    InvoiceMatchStatus.AMOUNT_MISMATCH,
)


class InvoiceSource(enum.StrEnum):
    """Which file the row came from. Required to render a side-by-side diff."""

    LEDGER = "LEDGER"
    GSTR2B = "GSTR2B"
    BOTH = "BOTH"


class ActionType(enum.StrEnum):
    VENDOR_NOTIFIED = "VENDOR_NOTIFIED"
    PAYMENT_HOLD_PROPOSED = "PAYMENT_HOLD_PROPOSED"
    PAYMENT_HOLD_APPLIED = "PAYMENT_HOLD_APPLIED"
    PAYMENT_RELEASED = "PAYMENT_RELEASED"
    MARKED_RESOLVED = "MARKED_RESOLVED"
    IGNORED = "IGNORED"


class CheckStatus(enum.StrEnum):
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RiskBand(enum.StrEnum):
    """Vendor filing-behaviour band. Thresholds live in config, not here."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"  # no filing history observed yet
