import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lockstep.database import Base


class Vendor(Base):
    __tablename__ = "vendors"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    # Null for an "unverified" vendor: a supplier who never filed and whose GSTIN a
    # Tally ledger never carried, so it could never be resolved by name against a 2B
    # row either. Uniqueness on a real GSTIN, and on `unverified_key` for a made-up
    # one, are both partial indexes — see the migration — so the two never collide.
    gstin: Mapped[str | None] = mapped_column(String(15), index=True)
    gstin_verified: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    unverified_key: Mapped[str | None] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_email: Mapped[str | None] = mapped_column(String(255))
    contact_phone: Mapped[str | None] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    invoices = relationship("Invoice", back_populates="vendor")
    filing_history = relationship(
        "VendorFilingHistory", back_populates="vendor", cascade="all, delete-orphan",
        passive_deletes=True,
    )
