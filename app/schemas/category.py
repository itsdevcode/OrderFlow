from datetime import datetime
from typing import ClassVar
from pydantic import BaseModel, ConfigDict, Field, model_validator


class CategoryCreate(BaseModel):
    name: str
    slug: str
    description: str | None = None


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None)
    slug: str | None = Field(default=None)
    description: str | None = None
    is_active: bool | None = Field(default=None)

    @model_validator(mode="after")
    def check_explicit_nulls(self):
        non_nullable_fields = {"name", "slug", "is_active"}
        for field in non_nullable_fields:
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be explicitly null")
        return self


class CategoryResponse(BaseModel):
    id: int
    name: str
    slug: str
    description: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime | None

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)
