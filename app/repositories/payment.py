from collections.abc import Sequence
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payment import Payment, PaymentStatus


class PaymentRepository:
    db: AsyncSession

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, payment_id: int) -> Payment | None:
        stmt = select(Payment).where(Payment.id == payment_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id_for_update(self, payment_id: int) -> Payment | None:
        stmt = select(Payment).where(Payment.id == payment_id).with_for_update()
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_idempotency_key(self, idempotency_key: str) -> Payment | None:
        stmt = select(Payment).where(Payment.idempotency_key == idempotency_key)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_idempotency_key_for_update(self, idempotency_key: str) -> Payment | None:
        stmt = select(Payment).where(Payment.idempotency_key == idempotency_key).with_for_update()
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_pending_by_order_id(self, order_id: int) -> Payment | None:
        stmt = select(Payment).where(
            Payment.order_id == order_id,
            Payment.status == PaymentStatus.PENDING,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_succeeded_by_order_id(self, order_id: int) -> Payment | None:
        stmt = select(Payment).where(
            Payment.order_id == order_id,
            Payment.status == PaymentStatus.SUCCEEDED,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_order_id(self, order_id: int) -> Sequence[Payment]:
        stmt = (
            select(Payment)
            .where(Payment.order_id == order_id)
            .order_by(Payment.created_at.desc(), Payment.id.desc())
        )
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def create(self, payment: Payment) -> Payment:
        self.db.add(payment)
        await self.db.flush()
        return payment
