from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.models.payment import PaymentStatus


class PaymentInitiateRequest(BaseModel):
    idempotency_key: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Unique client-supplied idempotency key for this payment attempt",
    )
    provider: str = Field(
        default="fake_provider",
        description="Payment provider name",
    )


class PaymentRead(BaseModel):
    id: int
    order_id: int
    provider: str
    provider_payment_id: str | None = None
    idempotency_key: str
    amount: Decimal
    currency: str
    status: PaymentStatus
    created_at: datetime
    updated_at: datetime | None = None

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)
