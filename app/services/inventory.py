from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from app.models.inventory import Inventory
from app.repositories.inventory import InventoryRepository
from app.exceptions.inventory import InventoryNotFoundError, InsufficientStockError
from app.schemas.inventory import InventoryAdjustment

class InventoryService:
    db: AsyncSession
    inventory_repo: InventoryRepository

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.inventory_repo = InventoryRepository(db)

    async def get_inventory(self, product_id: int, warehouse_id: int) -> Inventory:
        inventory = await self.inventory_repo.get_by_product_and_warehouse(product_id, warehouse_id)
        if not inventory:
            raise InventoryNotFoundError(f"Inventory for product {product_id} at warehouse {warehouse_id} not found")
        return inventory

    async def list_product_inventory(self, product_id: int, offset: int = 0, limit: int = 20) -> list[Inventory]:
        return await self.inventory_repo.list_by_product(product_id, offset=offset, limit=limit)

    async def adjust_stock(self, product_id: int, warehouse_id: int, adjustment: InventoryAdjustment) -> Inventory:
        # Fetch or create
        inventory = await self.inventory_repo.get_by_product_and_warehouse(product_id, warehouse_id)
        if not inventory:
            # We create it if it doesn't exist to allow initialization
            try:
                inventory = await self.inventory_repo.create(product_id, warehouse_id)
            except IntegrityError as exc:
                # E.g. invalid product_id or warehouse_id
                await self.db.rollback()
                raise ValueError("Invalid product or warehouse ID") from exc
        
        # Apply adjustment
        inventory.available_quantity += adjustment.available_quantity_change
        inventory.reserved_quantity += adjustment.reserved_quantity_change

        if inventory.available_quantity < 0:
            raise InsufficientStockError("Available quantity cannot be negative")
        if inventory.reserved_quantity < 0:
            raise InsufficientStockError("Reserved quantity cannot be negative")

        try:
            await self.db.commit()
            await self.db.refresh(inventory)
        except IntegrityError as exc:
            await self.db.rollback()
            raise InsufficientStockError("Database integrity error adjusting stock") from exc

        return inventory
