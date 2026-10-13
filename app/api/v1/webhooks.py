from fastapi import APIRouter, Header, Request, status
from app.core.config import settings
from app.dependencies.database import DbSession
from app.exceptions.webhook import WebhookPayloadTooLargeError
from app.schemas.webhook import WebhookResponse
from app.services.payment_webhook import PaymentWebhookService

router = APIRouter(prefix="/webhooks/payments", tags=["Webhooks"])


@router.post(
    "",
    response_model=WebhookResponse,
    status_code=status.HTTP_200_OK,
    summary="Process Payment Webhook",
    description="Secure, idempotent webhook endpoint for payment provider events. Uses HMAC-SHA256 signature verification.",
)
async def process_payment_webhook(
    request: Request,
    db: DbSession,
    x_webhook_signature: str | None = Header(default=None, alias="X-Webhook-Signature"),
) -> WebhookResponse:
    # 1. Enforce payload size limit
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > settings.payment_webhook_max_bytes:
        raise WebhookPayloadTooLargeError(
            f"Payload length {content_length} exceeds limit of {settings.payment_webhook_max_bytes} bytes"
        )

    # 2. Read raw bytes
    raw_body = await request.body()
    if len(raw_body) > settings.payment_webhook_max_bytes:
        raise WebhookPayloadTooLargeError(
            f"Payload size {len(raw_body)} exceeds limit of {settings.payment_webhook_max_bytes} bytes"
        )

    # 3. Process webhook
    service = PaymentWebhookService(db)
    return await service.process_webhook(
        raw_body=raw_body,
        signature_header=x_webhook_signature,
    )
