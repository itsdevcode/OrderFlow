from datetime import datetime
from typing import ClassVar
from pydantic import BaseModel, ConfigDict

class InventoryResponse(BaseModel):
    id: int
    product_id: int
    warehouse_id: int
    available_quantity: int
    reserved_quantity: int
    created_at: datetime
    updated_at: datetime | None

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

class InventoryAdjustment(BaseModel):
    available_quantity_change: int = 0
    reserved_quantity_change: int = 0
