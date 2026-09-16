"""add fe columns to invoices

Revision ID: a2f3b9c1d4e5
Revises: 18dc7bd309a2
Create Date: 2026-09-16 16:56:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a2f3b9c1d4e5"
down_revision: Union[str, None] = "18dc7bd309a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("action_status", sa.String(20), server_default="none", nullable=False))
    op.add_column("invoices", sa.Column("activity_log", postgresql.JSONB, server_default="[]", nullable=False))
    op.add_column("invoices", sa.Column("match_confidence", sa.Integer, server_default="0"))
    op.add_column("invoices", sa.Column("ai_suggestions", postgresql.JSONB, server_default="[]", nullable=False))
    op.add_column("invoices", sa.Column("purchase_data", postgresql.JSONB))
    op.add_column("invoices", sa.Column("gstr2b_data", postgresql.JSONB))

    # Make created_by nullable on reconciliation_runs so FE runs (no auth) can be created
    op.alter_column("reconciliation_runs", "created_by", nullable=True)


def downgrade() -> None:
    op.alter_column("reconciliation_runs", "created_by", nullable=False)
    op.drop_column("invoices", "gstr2b_data")
    op.drop_column("invoices", "purchase_data")
    op.drop_column("invoices", "ai_suggestions")
    op.drop_column("invoices", "match_confidence")
    op.drop_column("invoices", "activity_log")
    op.drop_column("invoices", "action_status")
