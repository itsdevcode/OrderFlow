from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.product import ProductRead


class CartItemAdd(BaseModel):
    product_id: int
    quantity: int = Field(default=1, gt=0)


class CartItemUpdate(BaseModel):
    quantity: int = Field(gt=0)


class CartItemRead(BaseModel):
    id: int
    cart_id: int
    product_id: int
    quantity: int
    created_at: datetime
    updated_at: datetime | None = None
    product: ProductRead

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)


class CartRead(BaseModel):
    id: int
    user_id: int
    created_at: datetime
    updated_at: datetime | None = None
    items: list[CartItemRead] = []

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)
