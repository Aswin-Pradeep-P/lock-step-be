import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lockstep.database import Base
from lockstep.models.enums import InvoiceMatchStatus, InvoiceRiskTier

invoice_match_status_enum = PGEnum(
    InvoiceMatchStatus,
    name="invoice_match_status",
    values_callable=lambda e: [m.value for m in e],
    create_type=False,
)

invoice_risk_tier_enum = PGEnum(
    InvoiceRiskTier,
    name="invoice_risk_tier",
    values_callable=lambda e: [m.value for m in e],
    create_type=False,
)


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reconciliation_runs.id", ondelete="CASCADE"), nullable=False
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL")
    )

    invoice_number: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[InvoiceMatchStatus] = mapped_column(
        invoice_match_status_enum, default=InvoiceMatchStatus.PENDING,
        server_default=InvoiceMatchStatus.PENDING.value,
    )
    risk_tier: Mapped[InvoiceRiskTier] = mapped_column(
        invoice_risk_tier_enum, default=InvoiceRiskTier.PENDING,
        server_default=InvoiceRiskTier.PENDING.value,
    )
    ai_summary: Mapped[str | None] = mapped_column(Text)
    citations: Mapped[list | None] = mapped_column(JSONB)
    recoverable_until: Mapped[date | None] = mapped_column(Date)
    itc_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))

    raw_data: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # FE-facing fields
    action_status: Mapped[str] = mapped_column(String(20), default="none", server_default="none")
    activity_log: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    match_confidence: Mapped[int | None] = mapped_column(default=0, server_default="0")
    ai_suggestions: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    purchase_data: Mapped[dict | None] = mapped_column(JSONB)
    gstr2b_data: Mapped[dict | None] = mapped_column(JSONB)

    last_reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    run = relationship("ReconciliationRun", back_populates="invoices")
    vendor = relationship("Vendor", back_populates="invoices")
