"""add payments table

Revision ID: c8f3a91bc7d2
Revises: 4b43faa5ba1b
Create Date: 2026-10-13 16:51:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8f3a91bc7d2'
down_revision: str | Sequence[str] | None = '4b43faa5ba1b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _ = op.create_table(
        'payments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=False),
        sa.Column('provider', sa.String(), nullable=False),
        sa.Column('provider_payment_id', sa.String(), nullable=True),
        sa.Column('idempotency_key', sa.String(), nullable=False),
        sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('currency', sa.String(), server_default='INR', nullable=False),
        sa.Column(
            'status',
            sa.Enum('PENDING', 'SUCCEEDED', 'FAILED', 'REFUNDED', name='paymentstatus_enum', native_enum=False),
            nullable=False
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.CheckConstraint('amount >= 0', name='chk_payment_amount_non_negative'),
        sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_payments_order_id'), 'payments', ['order_id'], unique=False)
    op.create_index(op.f('ix_payments_provider_payment_id'), 'payments', ['provider_payment_id'], unique=False)
    op.create_index(op.f('ix_payments_idempotency_key'), 'payments', ['idempotency_key'], unique=True)
    op.create_index(op.f('ix_payments_status'), 'payments', ['status'], unique=False)
    op.create_index(
        'uq_payments_order_id_pending',
        'payments',
        ['order_id'],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'")
    )
    op.create_index(
        'uq_payments_order_id_succeeded',
        'payments',
        ['order_id'],
        unique=True,
        postgresql_where=sa.text("status = 'SUCCEEDED'")
    )


def downgrade() -> None:
    op.drop_index('uq_payments_order_id_succeeded', table_name='payments')
    op.drop_index('uq_payments_order_id_pending', table_name='payments')
    op.drop_index(op.f('ix_payments_status'), table_name='payments')
    op.drop_index(op.f('ix_payments_idempotency_key'), table_name='payments')
    op.drop_index(op.f('ix_payments_provider_payment_id'), table_name='payments')
    op.drop_index(op.f('ix_payments_order_id'), table_name='payments')
    op.drop_table('payments')
