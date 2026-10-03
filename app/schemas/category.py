from datetime import datetime
from typing import ClassVar
from pydantic import BaseModel, ConfigDict, Field


class CategoryCreate(BaseModel):
    name: str
    slug: str
    description: str | None = None


class CategoryUpdate(BaseModel):
    name: str = Field(default=None)  # pyright: ignore[reportAssignmentType]
    slug: str = Field(default=None)  # pyright: ignore[reportAssignmentType]
    description: str | None = None
    is_active: bool = Field(default=None)  # pyright: ignore[reportAssignmentType]


class CategoryResponse(BaseModel):
    id: int
    name: str
    slug: str
    description: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime | None

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)
