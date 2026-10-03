from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.warehouse import Warehouse

class WarehouseRepository:
    db: AsyncSession

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, warehouse_id: int) -> Warehouse | None:
        result = await self.db.execute(select(Warehouse).where(Warehouse.id == warehouse_id))
        return result.scalar_one_or_none()

    async def get_by_code(self, code: str) -> Warehouse | None:
        result = await self.db.execute(select(Warehouse).where(Warehouse.code == code))
        return result.scalar_one_or_none()

    async def list(self, *, offset: int = 0, limit: int = 20) -> list[Warehouse]:
        result = await self.db.execute(
            select(Warehouse).order_by(Warehouse.created_at.desc()).offset(offset).limit(limit)
        )
        return list(result.scalars().all())

    async def create(self, *, code: str, name: str, address: str | None = None, is_active: bool = True) -> Warehouse:
        warehouse = Warehouse(code=code, name=name, address=address, is_active=is_active)
        self.db.add(warehouse)
        await self.db.flush()
        await self.db.refresh(warehouse)
        return warehouse

    async def update(self, warehouse: Warehouse, **changes: str | bool | None) -> Warehouse:
        allowed_fields = {"code", "name", "address", "is_active"}
        for key, value in changes.items():
            if key in allowed_fields:
                setattr(warehouse, key, value)
        await self.db.flush()
        await self.db.refresh(warehouse)
        return warehouse

    async def delete(self, warehouse: Warehouse) -> None:
        await self.db.delete(warehouse)
        await self.db.flush()
