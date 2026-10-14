from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.shipment import ShipmentStatus


class ShipmentCreate(BaseModel):
    carrier: str | None = Field(default=None, max_length=100)
    tracking_number: str | None = Field(default=None, max_length=100)

    @field_validator("carrier", "tracking_number", mode="before")
    @classmethod
    def strip_and_validate_optional_string(cls, v: object) -> str | None:
        if v is None:
            return None
        if isinstance(v, str):
            v_stripped = v.strip()
            return v_stripped if v_stripped else None
        return str(v).strip() or None

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class ShipmentStatusUpdate(BaseModel):
    status: ShipmentStatus
    carrier: str | None = Field(default=None, max_length=100)
    tracking_number: str | None = Field(default=None, max_length=100)

    @field_validator("carrier", "tracking_number", mode="before")
    @classmethod
    def strip_string(cls, v: object) -> str | None:
        if v is None:
            return None
        if isinstance(v, str):
            v_stripped = v.strip()
            return v_stripped if v_stripped else None
        return str(v).strip() or None

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class ShipmentRead(BaseModel):
    id: int
    order_id: int
    tracking_number: str | None = None
    carrier: str | None = None
    status: ShipmentStatus
    shipped_at: datetime | None = None
    delivered_at: datetime | None = None
    cancelled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)
