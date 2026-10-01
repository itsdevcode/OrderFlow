from typing import ClassVar
from pydantic import BaseModel, ConfigDict, EmailStr


class UserCreate(BaseModel):
    name: str
    email: EmailStr
    password: str
    role_id: int


class UserRead(BaseModel):
    id: int
    name: str
    email: EmailStr
    is_active: bool
    role_id: int

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)