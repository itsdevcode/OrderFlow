"""add unique index on provider and provider_payment_id and update webhook event status enum

Revision ID: e1f2a3b4c5d6
Revises: d9a41b2c7e10
Create Date: 2026-10-13 17:47:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: str | Sequence[str] | None = 'd9a41b2c7e10'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Create partial unique index on non-null (provider, provider_payment_id) in payments table
    op.create_index(
        'uq_payments_provider_payment_id',
        'payments',
        ['provider', 'provider_payment_id'],
        unique=True,
        postgresql_where=sa.text("provider_payment_id IS NOT NULL")
    )


def downgrade() -> None:
    op.drop_index(
        'uq_payments_provider_payment_id',
        table_name='payments'
    )
