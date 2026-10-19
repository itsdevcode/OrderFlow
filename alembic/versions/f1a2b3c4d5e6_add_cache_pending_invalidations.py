"""add_cache_pending_invalidations

Revision ID: f1a2b3c4d5e6
Revises: 44ab39d83482
Create Date: 2026-10-19 18:07:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: str | Sequence[str] | None = '44ab39d83482'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    _ = op.create_table(
        'cache_pending_invalidations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(length=255), nullable=True),
        sa.Column('namespace', sa.String(length=50), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_cache_pending_invalidations_key'), 'cache_pending_invalidations', ['key'], unique=False)
    op.create_index(op.f('ix_cache_pending_invalidations_namespace'), 'cache_pending_invalidations', ['namespace'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_cache_pending_invalidations_namespace'), table_name='cache_pending_invalidations')
    op.drop_index(op.f('ix_cache_pending_invalidations_key'), table_name='cache_pending_invalidations')
    op.drop_table('cache_pending_invalidations')
