import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lockstep.database import Base
from lockstep.models.enums import InvoiceMatchStatus, InvoiceSource

invoice_match_status_enum = PGEnum(
    InvoiceMatchStatus,
    name="invoice_match_status",
    values_callable=lambda e: [m.value for m in e],
    create_type=False,
)

invoice_source_enum = PGEnum(
    InvoiceSource,
    name="invoice_source",
    values_callable=lambda e: [m.value for m in e],
    create_type=False,
)

_MONEY = Numeric(14, 2)


class Invoice(Base):
    """One invoice row from one side of one check.

    A matched pair is two rows pointing at each other via `matched_invoice_id`, so the UI
    can render ledger-vs-2B side by side. Amounts are real columns (not JSONB) because
    exposure has to be a plain SUM.
    """

    __tablename__ = "invoices"
    __table_args__ = (
        Index("idx_inv_period_status", "period_id", "status"),
        Index("idx_inv_vendor", "vendor_id"),
        # The hot query is "what is at risk right now", not "show me everything".
        Index(
            "idx_inv_exposure",
            "period_id",
            postgresql_where=text(
                "status IN ('MISSING_IN_GSTR2B','AMOUNT_MISMATCH')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    check_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reconciliation_checks.id", ondelete="CASCADE"),
        nullable=False,
    )
    period_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reconciliation_periods.id", ondelete="CASCADE"),
        nullable=False,
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL")
    )
    # Survives an unmapped vendor — a ledger row for a GSTIN we've never seen still has
    # somewhere to put it.
    vendor_gstin: Mapped[str | None] = mapped_column(String(15), index=True)

    source: Mapped[InvoiceSource] = mapped_column(invoice_source_enum, nullable=False)
    matched_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="SET NULL")
    )

    invoice_number: Mapped[str] = mapped_column(String(100), nullable=False)
    invoice_date: Mapped[date | None] = mapped_column(Date)
    taxable_value: Mapped[Decimal | None] = mapped_column(_MONEY)
    igst: Mapped[Decimal] = mapped_column(_MONEY, default=Decimal("0"), server_default="0")
    cgst: Mapped[Decimal] = mapped_column(_MONEY, default=Decimal("0"), server_default="0")
    sgst: Mapped[Decimal] = mapped_column(_MONEY, default=Decimal("0"), server_default="0")
    cess: Mapped[Decimal] = mapped_column(_MONEY, default=Decimal("0"), server_default="0")

    itc_available: Mapped[bool | None] = mapped_column(Boolean)  # 2B 'ITC Availability'
    itc_reason: Mapped[str | None] = mapped_column(String(255))  # 2B 'Reason'
    is_reverse_charge: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    # When the supplier actually filed GSTR-1. The most valuable column in the 2B file.
    supplier_filed_at: Mapped[date | None] = mapped_column(Date)

    status: Mapped[InvoiceMatchStatus] = mapped_column(
        invoice_match_status_enum, default=InvoiceMatchStatus.PENDING,
        server_default=InvoiceMatchStatus.PENDING.value, nullable=False,
    )
    # A human sentence, not a code. Deterministic — a CA must be able to audit it.
    # 500, not 255: a Sec 17(5) category hint (see risk_rules.sec17_5_hint) appends a
    # full second sentence onto the base reason for a MISSING_IN_GSTR2B invoice.
    match_reason: Mapped[str | None] = mapped_column(String(500))
    carried_from_period: Mapped[str | None] = mapped_column(CHAR(6))
    # Sec 16(4) backstop. Secondary information, not the headline, and not the LLM's call.
    recoverable_until: Mapped[date | None] = mapped_column(Date)
    # The ledger/2B narration, if the file carried one — the only thing that lets a
    # Sec 17(5) category hint (see risk_rules.sec17_5_hint) survive past match time.
    description: Mapped[str | None] = mapped_column(String(500))

    raw_data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    check = relationship("ReconciliationCheck", back_populates="invoices")
    vendor = relationship("Vendor", back_populates="invoices")
    actions = relationship(
        "InvoiceAction", back_populates="invoice", cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def total_tax(self) -> Decimal:
        return (self.igst or 0) + (self.cgst or 0) + (self.sgst or 0) + (self.cess or 0)
