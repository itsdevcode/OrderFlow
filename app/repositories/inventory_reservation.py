from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.inventory_reservation import InventoryReservation

class InventoryReservationRepository:
    db: AsyncSession

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(self, reservation: InventoryReservation) -> InventoryReservation:
        self.db.add(reservation)
        await self.db.flush()
        return reservation

    async def get_by_id(self, reservation_id: int, *, for_update: bool = False) -> InventoryReservation | None:
        stmt = select(InventoryReservation).where(InventoryReservation.id == reservation_id)
        if for_update:
            stmt = stmt.with_for_update()
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_order_id(
        self,
        order_id: int,
        *,
        for_update: bool = False,
    ) -> list[InventoryReservation]:
        stmt = (
            select(InventoryReservation)
            .where(InventoryReservation.order_id == order_id)
            .order_by(InventoryReservation.product_id.asc(), InventoryReservation.warehouse_id.asc(), InventoryReservation.id.asc())
        )
        if for_update:
            stmt = stmt.with_for_update()
        result = await self.db.execute(stmt)
        return list(result.scalars().all())
