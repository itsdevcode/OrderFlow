from decimal import Decimal
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field


class PaymentWebhookPayload(BaseModel):
    """
    Contract for fake payment provider webhook events.
    Local/test-only contract, not a production gateway spec.
    """
    model_config = ConfigDict(extra="forbid")

    event_id: Annotated[str, Field(min_length=1, max_length=128, description="Provider unique event id")]
    event_type: Literal["payment.succeeded", "payment.failed"]
    provider: Annotated[str, Field(default="fake_provider", min_length=1, max_length=64)]
    provider_payment_id: Annotated[str, Field(min_length=1, max_length=128)]
    amount: Annotated[Decimal, Field(gt=Decimal("0.00"), decimal_places=2)]
    currency: Annotated[str, Field(default="INR", min_length=3, max_length=3)]
    timestamp: Annotated[int, Field(gt=0, description="Unix epoch timestamp in seconds")]


class WebhookResponse(BaseModel):
    status: str
    event_id: str
    message: str
