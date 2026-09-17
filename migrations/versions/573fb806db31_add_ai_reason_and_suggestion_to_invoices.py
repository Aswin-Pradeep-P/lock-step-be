"""add ai reason and suggestion to invoices

Revision ID: 573fb806db31
Revises: c3a8f1d20b47
Create Date: 2026-09-17 20:00:06.284926

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '573fb806db31'
down_revision: Union[str, None] = 'c3a8f1d20b47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('invoices', sa.Column('ai_reason_md', sa.Text(), nullable=True))
    op.add_column('invoices', sa.Column('ai_suggestion_md', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('invoices', 'ai_suggestion_md')
    op.drop_column('invoices', 'ai_reason_md')
