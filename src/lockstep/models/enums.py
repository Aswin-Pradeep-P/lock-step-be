import enum


class InvoiceMatchStatus(enum.StrEnum):
    """Matching-engine outcome for one invoice against GSTR-2B."""

    PENDING = "PENDING"
    EXACT_MATCH = "EXACT_MATCH"
    CLERICAL_MISMATCH = "CLERICAL_MISMATCH"
    MISSING_IN_GSTR2B = "MISSING_IN_GSTR2B"
    MISSING_IN_LEDGER = "MISSING_IN_LEDGER"


class InvoiceRiskTier(enum.StrEnum):
    """AI/rules risk verdict — drives the UI's filter buckets.

    Distinct from InvoiceMatchStatus: a MISSING_IN_GSTR2B invoice is HIGH_RISK
    only while the Sec 16(4) claim window is open, and becomes BLOCKED once
    that window closes or the expense category is ineligible under Sec 17(5).
    """

    PENDING = "PENDING"
    SAFE = "SAFE"
    LOW_RISK = "LOW_RISK"
    HIGH_RISK = "HIGH_RISK"
    BLOCKED = "BLOCKED"


class RunStatus(enum.StrEnum):
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
