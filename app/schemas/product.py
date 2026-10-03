from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProductCreate(BaseModel):
    name: str
    slug: str
    sku: str
    description: str | None = None
    price: Decimal = Field(ge=0)
    is_active: bool = True
    category_id: int


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None)
    slug: str | None = Field(default=None)
    sku: str | None = Field(default=None)
    description: str | None = None
    price: Decimal | None = Field(default=None, ge=0)   
    is_active: bool | None = Field(default=None)    
    category_id: int | None = Field(default=None)

    @model_validator(mode="after")
    def check_explicit_nulls(self):
        non_nullable_fields = {"name", "slug", "sku", "price", "is_active", "category_id"}
        for field in non_nullable_fields:
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be explicitly null")
        return self


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
