from typing import ClassVar
from pydantic import BaseModel, ConfigDict


class RoleCreate(BaseModel):
    name: str


class RoleRead(BaseModel):
    id: int
    name: str

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)