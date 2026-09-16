"""Unverified vendors, invoice description.

A supplier who has never filed and whose GSTIN a Tally ledger never carried can't be
resolved by name against a GSTR-2B row either (they're not in it) — today that row is
simply dropped, so there is nothing to hang a risk score or a reminder on for exactly
the vendors most worth chasing. Relaxes `vendors.gstin` to nullable and adds
`gstin_verified`/`unverified_key` so those vendors get a durable row instead, matched
across uploads by normalized name. Also persists `invoices.description` (parsed
already, previously dropped) so a Sec 17(5) category hint survives past match time.

Revision ID: 040290afdbf0
Revises: b2c7e51a4d16
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '040290afdbf0'
down_revision: Union[str, None] = 'b2c7e51a4d16'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('vendors', 'gstin', nullable=True)
    op.drop_constraint('vendors_gstin_key', 'vendors', type_='unique')
    op.add_column(
        'vendors',
        sa.Column('gstin_verified', sa.Boolean, nullable=False, server_default='true'),
    )
    op.add_column('vendors', sa.Column('unverified_key', sa.String(255)))
    op.create_index(
        'uq_vendors_gstin', 'vendors', ['gstin'], unique=True,
        postgresql_where=sa.text('gstin IS NOT NULL'),
    )
    op.create_index(
        'uq_vendors_unverified_key', 'vendors', ['unverified_key'], unique=True,
        postgresql_where=sa.text('gstin IS NULL'),
    )
    op.add_column('invoices', sa.Column('description', sa.String(500)))
    # A Sec 17(5) category hint appends a full second sentence onto the base
    # match_reason for a MISSING_IN_GSTR2B invoice — 255 wasn't enough headroom.
    op.alter_column('invoices', 'match_reason', type_=sa.String(500))


def downgrade() -> None:
    op.alter_column('invoices', 'match_reason', type_=sa.String(255))
    op.drop_column('invoices', 'description')
    op.drop_index('uq_vendors_unverified_key', table_name='vendors')
    op.drop_index('uq_vendors_gstin', table_name='vendors')
    op.drop_column('vendors', 'unverified_key')
    op.drop_column('vendors', 'gstin_verified')
    op.create_unique_constraint('vendors_gstin_key', 'vendors', ['gstin'])
    op.alter_column('vendors', 'gstin', nullable=False)
