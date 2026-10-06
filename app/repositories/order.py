from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.order import Order
from app.models.order_item import OrderItem


class OrderRepository:
    db: AsyncSession

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create_order(self, order: Order, items: list[OrderItem]) -> Order:
        self.db.add(order)
        # Flush to get the order ID
        await self.db.flush()
        
        for item in items:
            item.order_id = order.id
            self.db.add(item)
            
        await self.db.flush()
        
        # Refetch with eager loading for items
        stmt = select(Order).where(Order.id == order.id).options(selectinload(Order.items))
        result = await self.db.execute(stmt)
        return result.scalar_one()

    async def get_by_id_and_user_id(self, order_id: int, user_id: int) -> Order | None:
        stmt = (
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.id == order_id, Order.user_id == user_id)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_user_id(self, user_id: int, offset: int = 0, limit: int = 20) -> list[Order]:
        stmt = (
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.user_id == user_id)
            .order_by(Order.created_at.desc(), Order.id.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())
