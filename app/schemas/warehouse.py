from datetime import datetime
from typing import ClassVar
from pydantic import BaseModel, ConfigDict, model_validator

class WarehouseCreate(BaseModel):
    code: str
    name: str
    address: str | None = None
    is_active: bool = True

class WarehouseUpdate(BaseModel):
    code: str | None = None
    name: str | None = None
    address: str | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def check_explicit_nulls(self):
        non_nullable_fields = {"code", "name", "is_active"}
        for field in non_nullable_fields:
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be explicitly null")
        return self

class WarehouseResponse(BaseModel):
    id: int
    code: str
    name: str
    address: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime | None

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)
