from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.models.order import OrderStatus


class OrderItemRead(BaseModel):
    id: int
    order_id: int
    product_id: int
    product_name: str
    product_sku: str
    unit_price: Decimal
    quantity: int
    created_at: datetime

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)


class OrderRead(BaseModel):
    id: int
    order_number: str
    user_id: int
    status: OrderStatus
    created_at: datetime
    updated_at: datetime | None = None
    items: list[OrderItemRead] = Field(default_factory=list)

    @computed_field
    @property
    def total_amount(self) -> Decimal:
        return sum((item.unit_price * item.quantity for item in self.items), Decimal("0.00"))

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)
