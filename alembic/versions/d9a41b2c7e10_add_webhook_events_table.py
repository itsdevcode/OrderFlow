"""add webhook events table

Revision ID: d9a41b2c7e10
Revises: c8f3a91bc7d2
Create Date: 2026-10-13 17:30:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9a41b2c7e10'
down_revision: str | Sequence[str] | None = 'c8f3a91bc7d2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _ = op.create_table(
        'webhook_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('provider', sa.String(), nullable=False),
        sa.Column('provider_event_id', sa.String(), nullable=False),
        sa.Column('event_type', sa.String(), nullable=False),
        sa.Column('payment_id', sa.Integer(), nullable=True),
        sa.Column(
            'status',
            sa.Enum('RECEIVED', 'PROCESSED', 'FAILED', name='webhookeventstatus_enum', native_enum=False),
            nullable=False
        ),
        sa.Column('payload_hash', sa.String(length=64), nullable=True),
        sa.Column('error_message', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['payment_id'], ['payments.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider', 'provider_event_id', name='uq_webhook_events_provider_event_id')
    )
    op.create_index(op.f('ix_webhook_events_provider'), 'webhook_events', ['provider'], unique=False)
    op.create_index(op.f('ix_webhook_events_provider_event_id'), 'webhook_events', ['provider_event_id'], unique=False)
    op.create_index(op.f('ix_webhook_events_event_type'), 'webhook_events', ['event_type'], unique=False)
    op.create_index(op.f('ix_webhook_events_payment_id'), 'webhook_events', ['payment_id'], unique=False)
    op.create_index(op.f('ix_webhook_events_status'), 'webhook_events', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_webhook_events_status'), table_name='webhook_events')
    op.drop_index(op.f('ix_webhook_events_payment_id'), table_name='webhook_events')
    op.drop_index(op.f('ix_webhook_events_event_type'), table_name='webhook_events')
    op.drop_index(op.f('ix_webhook_events_provider_event_id'), table_name='webhook_events')
    op.drop_index(op.f('ix_webhook_events_provider'), table_name='webhook_events')
    op.drop_table('webhook_events')
