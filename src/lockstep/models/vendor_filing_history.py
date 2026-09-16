import uuid
from datetime import date, datetime

from sqlalchemy import (
    CHAR,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lockstep.database import Base


class VendorFilingHistory(Base):
    """Did this vendor file GSTR-1 before the 13th, this period? Accumulated across periods
    it is the only thing in the system that supports a *prediction* rather than a report.

    Core, not an extra: without it there is no "late in 3 of the last 4 periods".
    """

    __tablename__ = "vendor_filing_history"
    __table_args__ = (
        UniqueConstraint("vendor_id", "tax_period", name="uq_filing_history_vendor_period"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tax_period: Mapped[str] = mapped_column(CHAR(6), nullable=False)  # MMYYYY
    gstr1_filed: Mapped[bool | None] = mapped_column(Boolean)
    gstr1_filed_at: Mapped[date | None] = mapped_column(Date)
    #: Negative means filed before the 13th. Null means not filed (yet).
    days_past_cutoff: Mapped[int | None] = mapped_column(SmallInteger)
    invoice_count: Mapped[int | None] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    vendor = relationship("Vendor", back_populates="filing_history")
