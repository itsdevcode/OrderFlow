from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from app.models.warehouse import Warehouse
from app.repositories.warehouse import WarehouseRepository
from app.schemas.warehouse import WarehouseCreate, WarehouseUpdate
from app.exceptions.warehouse import WarehouseAlreadyExistsError, WarehouseNotFoundError, WarehouseInUseError

class WarehouseService:
    db: AsyncSession
    warehouse_repo: WarehouseRepository

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.warehouse_repo = WarehouseRepository(db)

    async def create_warehouse(self, data: WarehouseCreate) -> Warehouse:
        if await self.warehouse_repo.get_by_code(data.code):
            raise WarehouseAlreadyExistsError(f"Warehouse with code '{data.code}' already exists")

        try:
            warehouse = await self.warehouse_repo.create(
                code=data.code, name=data.name, address=data.address, is_active=data.is_active
            )
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise WarehouseAlreadyExistsError("Warehouse code already exists") from exc
        return warehouse

    async def list_warehouses(self, offset: int = 0, limit: int = 20) -> list[Warehouse]:
        return await self.warehouse_repo.list(offset=offset, limit=limit)

    async def get_warehouse(self, warehouse_id: int) -> Warehouse:
        warehouse = await self.warehouse_repo.get_by_id(warehouse_id)
        if not warehouse:
            raise WarehouseNotFoundError(f"Warehouse with id {warehouse_id} not found")
        return warehouse

    async def update_warehouse(self, warehouse_id: int, data: WarehouseUpdate) -> Warehouse:
        warehouse = await self.get_warehouse(warehouse_id)
        changes: dict[str, str | bool | None] = data.model_dump(exclude_unset=True)
        if not changes:
            return warehouse

        if "code" in changes and isinstance(changes["code"], str):
            existing = await self.warehouse_repo.get_by_code(changes["code"])
            if existing and existing.id != warehouse_id:
                raise WarehouseAlreadyExistsError(f"Warehouse with code '{changes['code']}' already exists")

        try:
            updated_warehouse = await self.warehouse_repo.update(warehouse, **changes)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise WarehouseAlreadyExistsError("Warehouse code already exists") from exc
        return updated_warehouse

    async def delete_warehouse(self, warehouse_id: int) -> None:
        warehouse = await self.get_warehouse(warehouse_id)
        try:
            await self.warehouse_repo.delete(warehouse)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise WarehouseInUseError("Cannot delete warehouse as it is currently in use") from exc
