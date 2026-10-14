"""add_shipments_table

Revision ID: 895934fcf2f9
Revises: e1f2a3b4c5d6
Create Date: 2026-10-14 18:09:46.015893

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '895934fcf2f9'
down_revision: str | Sequence[str] | None = 'e1f2a3b4c5d6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _ = op.create_table(
        'shipments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=False),
        sa.Column('tracking_number', sa.String(length=100), nullable=True),
        sa.Column('carrier', sa.String(length=100), nullable=True),
        sa.Column(
            'status',
            sa.Enum('PENDING', 'SHIPPED', 'DELIVERED', 'CANCELLED', name='shipmentstatus_enum', native_enum=False),
            nullable=False,
            server_default='PENDING',
        ),
        sa.Column('shipped_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('order_id', name='uq_shipments_order_id'),
    )
    op.create_index(op.f('ix_shipments_order_id'), 'shipments', ['order_id'], unique=True)
    op.create_index(op.f('ix_shipments_status'), 'shipments', ['status'], unique=False)
    op.create_index(
        'uq_shipments_carrier_tracking_number',
        'shipments',
        ['carrier', 'tracking_number'],
        unique=True,
        postgresql_where=sa.text('carrier IS NOT NULL AND tracking_number IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_shipments_carrier_tracking_number', table_name='shipments')
    op.drop_index(op.f('ix_shipments_status'), table_name='shipments')
    op.drop_index(op.f('ix_shipments_order_id'), table_name='shipments')
    op.drop_table('shipments')
