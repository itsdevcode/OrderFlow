from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class ProductCreate(BaseModel):
    name: str
    slug: str
    sku: str
    description: str | None = None
    price: Decimal
    is_active: bool = True
    category_id: int


class ProductUpdate(BaseModel):
    name: str | None = None
    slug: str | None = None
    sku: str | None = None
    description: str | None = None
    price: Decimal | None = None
    is_active: bool | None = None
    category_id: int | None = None


class ProductRead(BaseModel):
    id: int
    name: str
    slug: str
    sku: str
    description: str | None = None
    price: Decimal
    is_active: bool
    category_id: int
    created_at: datetime
    updated_at: datetime | None = None

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)
