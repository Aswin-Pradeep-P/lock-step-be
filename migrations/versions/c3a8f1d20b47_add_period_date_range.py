"""Add from_date and to_date to reconciliation_periods.

Revision ID: c3a8f1d20b47
Revises: 040290afdbf0
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c3a8f1d20b47'
down_revision: Union[str, None] = '040290afdbf0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('reconciliation_periods', sa.Column('from_date', sa.Date, nullable=True))
    op.add_column('reconciliation_periods', sa.Column('to_date', sa.Date, nullable=True))


def downgrade() -> None:
    op.drop_column('reconciliation_periods', 'to_date')
    op.drop_column('reconciliation_periods', 'from_date')
