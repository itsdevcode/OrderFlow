from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.inventory import Inventory

class InventoryRepository:
    db: AsyncSession

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, inventory_id: int) -> Inventory | None:
        result = await self.db.execute(select(Inventory).where(Inventory.id == inventory_id))
        return result.scalar_one_or_none()

    async def get_by_product_and_warehouse(self, product_id: int, warehouse_id: int, *, for_update: bool = False) -> Inventory | None:
        stmt = select(Inventory).where(
            Inventory.product_id == product_id,
            Inventory.warehouse_id == warehouse_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all_for_update_by_product(self, product_id: int) -> list[Inventory]:
        stmt = (
            select(Inventory)
            .where(Inventory.product_id == product_id)
            .order_by(Inventory.warehouse_id.asc())
            .with_for_update()
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_product(self, product_id: int, *, offset: int = 0, limit: int = 20) -> list[Inventory]:
        result = await self.db.execute(
            select(Inventory).where(Inventory.product_id == product_id).offset(offset).limit(limit)
        )
        return list(result.scalars().all())

    async def create(self, product_id: int, warehouse_id: int, available_quantity: int = 0, reserved_quantity: int = 0) -> Inventory:
        inventory = Inventory(
            product_id=product_id,
            warehouse_id=warehouse_id,
            available_quantity=available_quantity,
            reserved_quantity=reserved_quantity
        )
        self.db.add(inventory)
        await self.db.flush()
        await self.db.refresh(inventory)
        return inventory
