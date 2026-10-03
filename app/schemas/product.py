from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class ProductCreate(BaseModel):
    name: str
    slug: str
    sku: str
    description: str | None = None
    price: Decimal = Field(ge=0)
    is_active: bool = True
    category_id: int


class ProductUpdate(BaseModel):
    name: str = Field(default=None)  # pyright: ignore[reportAssignmentType]
    slug: str = Field(default=None)  # pyright: ignore[reportAssignmentType]
    sku: str = Field(default=None)  # pyright: ignore[reportAssignmentType]
    description: str | None = None
    price: Decimal = Field(default=None, ge=0)  # pyright: ignore[reportAssignmentType]
    is_active: bool = Field(default=None)  # pyright: ignore[reportAssignmentType]
    category_id: int = Field(default=None)  # pyright: ignore[reportAssignmentType]


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
