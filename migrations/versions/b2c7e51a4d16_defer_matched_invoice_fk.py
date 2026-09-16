"""Make invoices.matched_invoice_id deferrable.

A matched pair points at each other, so neither row can be inserted "first" under an
immediate constraint. Deferring to commit lets the pair be written in one flush
instead of forcing a two-phase insert on every caller.

Revision ID: b2c7e51a4d16
Revises: a1f4c2d8e903
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'b2c7e51a4d16'
down_revision: Union[str, None] = 'a1f4c2d8e903'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FK = 'invoices_matched_invoice_id_fkey'


def upgrade() -> None:
    op.execute(f'ALTER TABLE invoices DROP CONSTRAINT {FK}')
    op.execute(
        f'ALTER TABLE invoices ADD CONSTRAINT {FK} '
        f'FOREIGN KEY (matched_invoice_id) REFERENCES invoices(id) ON DELETE SET NULL '
        f'DEFERRABLE INITIALLY DEFERRED'
    )


def downgrade() -> None:
    op.execute(f'ALTER TABLE invoices DROP CONSTRAINT {FK}')
    op.execute(
        f'ALTER TABLE invoices ADD CONSTRAINT {FK} '
        f'FOREIGN KEY (matched_invoice_id) REFERENCES invoices(id) ON DELETE SET NULL'
    )
