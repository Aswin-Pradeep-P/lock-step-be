import uuid
from datetime import date, datetime

from sqlalchemy import (
    CHAR,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lockstep.database import Base
from lockstep.models.enums import CheckStatus


class Client(Base):
    """A business whose ITC we watch. A CA firm (org_id) has many."""

    __tablename__ = "clients"
    __table_args__ = (UniqueConstraint("org_id", "gstin", name="uq_clients_org_gstin"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    legal_name: Mapped[str] = mapped_column(String(255), nullable=False)
    gstin: Mapped[str] = mapped_column(String(15), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    periods = relationship("ReconciliationPeriod", back_populates="client", passive_deletes=True)


class ReconciliationPeriod(Base):
    """One client, one tax period. Holds many checks — the value is the delta between them."""

    __tablename__ = "reconciliation_periods"
    __table_args__ = (
        UniqueConstraint("client_id", "tax_period", name="uq_period_client_tax_period"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    tax_period: Mapped[str] = mapped_column(CHAR(6), nullable=False)  # MMYYYY
    cutoff_date: Mapped[date] = mapped_column(Date, nullable=False)  # the 13th
    gstr2b_date: Mapped[date] = mapped_column(Date, nullable=False)  # the 14th
    filing_due: Mapped[date] = mapped_column(Date, nullable=False)  # the 20th
    from_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # upload date range start
    to_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # upload date range end
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    client = relationship("Client", back_populates="periods")
    checks = relationship(
        "ReconciliationCheck", back_populates="period", cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ReconciliationCheck(Base):
    """One upload-and-match pass within a period. Re-run as often as you like before the 13th."""

    __tablename__ = "reconciliation_checks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    period_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reconciliation_periods.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    ledger_file_url: Mapped[str | None] = mapped_column(Text)
    gstr2b_file_url: Mapped[str | None] = mapped_column(Text)
    column_mapping: Mapped[dict | None] = mapped_column(JSONB)
    rows_parsed: Mapped[int | None] = mapped_column(Integer)
    parse_errors: Mapped[list | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default=CheckStatus.PROCESSING.value,
        server_default=CheckStatus.PROCESSING.value,
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    period = relationship("ReconciliationPeriod", back_populates="checks")
    invoices = relationship(
        "Invoice", back_populates="check", cascade="all, delete-orphan", passive_deletes=True
    )
