"""Period model, filing history, actions — realign schema to PROJECT brief.

Replaces the one-shot `reconciliation_runs` model with
`clients -> reconciliation_periods -> reconciliation_checks -> invoices`, adds
`vendor_filing_history` (the table the product's differentiation rests on) and
`invoice_actions` (the audit trail), and moves invoice amounts out of JSONB into
real NUMERIC(14,2) columns.

`invoices` and `reconciliation_runs` are dropped and rebuilt rather than altered:
their shape changes almost entirely, and there is no production data.

Revision ID: a1f4c2d8e903
Revises: 18dc7bd309a2
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a1f4c2d8e903'
down_revision: Union[str, None] = '18dc7bd309a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MATCH_STATUSES = (
    'PENDING', 'EXACT_MATCH', 'CLERICAL_MISMATCH', 'AMOUNT_MISMATCH',
    'MISSING_IN_GSTR2B', 'MISSING_IN_LEDGER', 'DUPLICATE',
    'ITC_INELIGIBLE', 'RESOLVED', 'CARRIED_FORWARD',
)
INVOICE_SOURCES = ('LEDGER', 'GSTR2B', 'BOTH')
ACTION_TYPES = (
    'VENDOR_NOTIFIED', 'PAYMENT_HOLD_PROPOSED', 'PAYMENT_HOLD_APPLIED',
    'PAYMENT_RELEASED', 'MARKED_RESOLVED', 'IGNORED',
)

invoice_match_status = postgresql.ENUM(*MATCH_STATUSES, name='invoice_match_status', create_type=False)
invoice_source = postgresql.ENUM(*INVOICE_SOURCES, name='invoice_source', create_type=False)
action_type = postgresql.ENUM(*ACTION_TYPES, name='action_type', create_type=False)

MONEY = sa.Numeric(14, 2)

# DEFAULT NOW() only fires on insert, and SQLAlchemy's onupdate misses writes that
# bypass the ORM (seed scripts, manual fixes, bulk updates).
TOUCH_FN = """
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.drop_table('invoices')
    op.drop_table('reconciliation_runs')
    op.execute('DROP TYPE IF EXISTS invoice_risk_tier')
    op.execute('DROP TYPE IF EXISTS invoice_match_status')

    bind = op.get_bind()
    invoice_match_status.create(bind, checkfirst=True)
    invoice_source.create(bind, checkfirst=True)
    action_type.create(bind, checkfirst=True)
    op.execute(TOUCH_FN)

    op.create_table(
        'clients',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('org_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('legal_name', sa.String(255), nullable=False),
        sa.Column('gstin', sa.String(15), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('org_id', 'gstin', name='uq_clients_org_gstin'),
    )
    op.create_index('idx_clients_org_id', 'clients', ['org_id'])

    op.create_table(
        'reconciliation_periods',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('client_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('clients.id', ondelete='CASCADE'), nullable=False),
        sa.Column('tax_period', sa.CHAR(6), nullable=False),
        sa.Column('cutoff_date', sa.Date, nullable=False),
        sa.Column('gstr2b_date', sa.Date, nullable=False),
        sa.Column('filing_due', sa.Date, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('client_id', 'tax_period', name='uq_period_client_tax_period'),
    )

    op.create_table(
        'reconciliation_checks',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('period_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('reconciliation_periods.id', ondelete='CASCADE'), nullable=False),
        sa.Column('ledger_file_url', sa.Text),
        sa.Column('gstr2b_file_url', sa.Text),
        sa.Column('column_mapping', postgresql.JSONB),
        sa.Column('rows_parsed', sa.Integer),
        sa.Column('parse_errors', postgresql.JSONB),
        sa.Column('status', sa.String(50), nullable=False, server_default='PROCESSING'),
        sa.Column('error_message', sa.Text),
        sa.Column('created_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('idx_checks_period_id', 'reconciliation_checks', ['period_id'])

    op.create_table(
        'invoices',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('check_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('reconciliation_checks.id', ondelete='CASCADE'), nullable=False),
        sa.Column('period_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('reconciliation_periods.id', ondelete='CASCADE'), nullable=False),
        sa.Column('vendor_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('vendors.id', ondelete='SET NULL')),
        sa.Column('vendor_gstin', sa.String(15)),
        sa.Column('source', invoice_source, nullable=False),
        sa.Column('matched_invoice_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('invoices.id', ondelete='SET NULL')),
        sa.Column('invoice_number', sa.String(100), nullable=False),
        sa.Column('invoice_date', sa.Date),
        sa.Column('taxable_value', MONEY),
        sa.Column('igst', MONEY, server_default='0'),
        sa.Column('cgst', MONEY, server_default='0'),
        sa.Column('sgst', MONEY, server_default='0'),
        sa.Column('cess', MONEY, server_default='0'),
        sa.Column('itc_available', sa.Boolean),
        sa.Column('itc_reason', sa.String(255)),
        sa.Column('is_reverse_charge', sa.Boolean, server_default='false'),
        sa.Column('supplier_filed_at', sa.Date),
        sa.Column('status', invoice_match_status, nullable=False, server_default='PENDING'),
        sa.Column('match_reason', sa.String(255)),
        sa.Column('carried_from_period', sa.CHAR(6)),
        sa.Column('recoverable_until', sa.Date),
        sa.Column('raw_data', postgresql.JSONB, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('idx_inv_period_status', 'invoices', ['period_id', 'status'])
    op.create_index('idx_inv_vendor', 'invoices', ['vendor_id'])
    op.create_index('idx_inv_vendor_gstin', 'invoices', ['vendor_gstin'])
    # The hot query is "what is at risk right now".
    op.create_index(
        'idx_inv_exposure', 'invoices', ['period_id'],
        postgresql_where=sa.text("status IN ('MISSING_IN_GSTR2B','AMOUNT_MISMATCH')"),
    )

    op.create_table(
        'vendor_filing_history',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('vendor_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('vendors.id', ondelete='CASCADE'), nullable=False),
        sa.Column('tax_period', sa.CHAR(6), nullable=False),
        sa.Column('gstr1_filed', sa.Boolean),
        sa.Column('gstr1_filed_at', sa.Date),
        sa.Column('days_past_cutoff', sa.SmallInteger),
        sa.Column('invoice_count', sa.Integer),
        sa.Column('observed_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('vendor_id', 'tax_period', name='uq_filing_history_vendor_period'),
    )
    op.create_index('idx_filing_history_vendor', 'vendor_filing_history', ['vendor_id'])

    op.create_table(
        'invoice_actions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('invoice_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('invoices.id', ondelete='CASCADE'), nullable=False),
        sa.Column('action', action_type, nullable=False),
        sa.Column('channel', sa.String(30)),
        sa.Column('auto_proposed', sa.Boolean, server_default='false'),
        sa.Column('approved_by', postgresql.UUID(as_uuid=True)),
        sa.Column('approved_at', sa.DateTime(timezone=True)),
        sa.Column('amount_at_risk', MONEY),
        sa.Column('payload', postgresql.JSONB),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('idx_actions_invoice', 'invoice_actions', ['invoice_id'])

    for table in ('invoices', 'vendors'):
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    for table in ('invoices', 'vendors'):
        op.execute(f'DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}')
    op.execute('DROP FUNCTION IF EXISTS set_updated_at()')
    op.drop_table('invoice_actions')
    op.drop_table('vendor_filing_history')
    op.drop_table('invoices')
    op.drop_table('reconciliation_checks')
    op.drop_table('reconciliation_periods')
    op.drop_table('clients')
    op.execute('DROP TYPE IF EXISTS action_type')
    op.execute('DROP TYPE IF EXISTS invoice_source')
    op.execute('DROP TYPE IF EXISTS invoice_match_status')
    # Leaves the 18dc7bd309a2 tables absent; that revision's downgrade is the rebuild path.
