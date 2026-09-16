import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lockstep.database import Base
from lockstep.models.enums import ActionType

action_type_enum = PGEnum(
    ActionType,
    name="action_type",
    values_callable=lambda e: [m.value for m in e],
    create_type=False,
)


class InvoiceAction(Base):
    """Append-only audit trail: who did what to this invoice, when, and who approved it.

    Never updated in place — the trail is the thing a CA is actually buying.
    """

    __tablename__ = "invoice_actions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    action: Mapped[ActionType] = mapped_column(action_type_enum, nullable=False)
    channel: Mapped[str | None] = mapped_column(String(30))  # email, whatsapp, portal
    auto_proposed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    amount_at_risk: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    payload: Mapped[dict | None] = mapped_column(JSONB)  # e.g. the generated email draft
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    invoice = relationship("Invoice", back_populates="actions")
