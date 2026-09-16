"""initial schema

Revision ID: 18dc7bd309a2
Revises:
Create Date: 2026-09-15 23:33:50.851958

"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '18dc7bd309a2'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

invoice_match_status = postgresql.ENUM(
    "PENDING", "EXACT_MATCH", "CLERICAL_MISMATCH", "MISSING_IN_GSTR2B", "MISSING_IN_LEDGER",
    name="invoice_match_status",
    create_type=False,  # created explicitly below; must not be auto-(re)created by create_table
)
invoice_risk_tier = postgresql.ENUM(
    "PENDING", "SAFE", "LOW_RISK", "HIGH_RISK", "BLOCKED",
    name="invoice_risk_tier",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    invoice_match_status.create(bind, checkfirst=True)
    invoice_risk_tier.create(bind, checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_users_email", "users", ["email"])

    op.create_table(
        "vendors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("gstin", sa.String(15), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("contact_email", sa.String(255)),
        sa.Column("contact_phone", sa.String(20)),
        sa.Column("is_active", sa.Boolean, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_vendors_gstin", "vendors", ["gstin"])

    op.create_table(
        "reconciliation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("invoice_ledger_file_url", sa.Text, nullable=False),
        sa.Column("gstr2b_file_url", sa.Text, nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(50), server_default="PROCESSING", nullable=False),
        sa.Column("error_message", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "invoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "run_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reconciliation_runs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "vendor_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vendors.id", ondelete="SET NULL"),
        ),
        sa.Column("invoice_number", sa.String(100), nullable=False),
        sa.Column("status", invoice_match_status, server_default="PENDING", nullable=False),
        sa.Column("risk_tier", invoice_risk_tier, server_default="PENDING", nullable=False),
        sa.Column("ai_summary", sa.Text),
        sa.Column("citations", postgresql.JSONB),
        sa.Column("recoverable_until", sa.Date),
        sa.Column("itc_amount", sa.Numeric(14, 2)),
        sa.Column("raw_data", postgresql.JSONB, nullable=False),
        sa.Column("last_reminder_sent_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_invoices_run_id", "invoices", ["run_id"])
    op.create_index("idx_invoices_vendor_id", "invoices", ["vendor_id"])
    op.create_index("idx_invoices_status", "invoices", ["status"])
    op.create_index("idx_invoices_risk_tier", "invoices", ["risk_tier"])
    op.create_index(
        "idx_invoices_raw_data", "invoices", ["raw_data"], postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_table("invoices")
    op.drop_table("reconciliation_runs")
    op.drop_table("vendors")
    op.drop_table("users")

    bind = op.get_bind()
    invoice_risk_tier.drop(bind, checkfirst=True)
    invoice_match_status.drop(bind, checkfirst=True)
