"""add_claim_fields_to_cache_pending_invalidations

Revision ID: a1b2c3d4e5f6
Revises: f1a2b3c4d5e6
Create Date: 2026-10-20 16:10:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: str | Sequence[str] | None = 'f1a2b3c4d5e6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('cache_pending_invalidations', sa.Column('claim_token', sa.String(length=255), nullable=True))
    op.add_column('cache_pending_invalidations', sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f('ix_cache_pending_invalidations_claimed_at'), 'cache_pending_invalidations', ['claimed_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_cache_pending_invalidations_claimed_at'), table_name='cache_pending_invalidations')
    op.drop_column('cache_pending_invalidations', 'claimed_at')
    op.drop_column('cache_pending_invalidations', 'claim_token')
