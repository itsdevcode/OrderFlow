from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request, status
from app.core.config import settings
from app.dependencies.database import DbSession
from app.dependencies.rate_limit import limit_payment_webhook
from app.exceptions.webhook import WebhookPayloadTooLargeError
from app.schemas.webhook import WebhookResponse
from app.services.payment_webhook import PaymentWebhookService

router = APIRouter(prefix="/webhooks/payments", tags=["Webhooks"])


@router.post(
    "",
    dependencies=[Depends(limit_payment_webhook)],
    response_model=WebhookResponse,
    status_code=status.HTTP_200_OK,
    summary="Process Payment Webhook",
    description="Secure, idempotent webhook endpoint for payment provider events. Uses HMAC-SHA256 signature verification.",
)
async def process_payment_webhook(
    request: Request,
    db: DbSession,
    background_tasks: BackgroundTasks,
    x_webhook_signature: str | None = Header(default=None, alias="X-Webhook-Signature"),
) -> WebhookResponse:
    # 1. Enforce payload size limit from Content-Length if present and valid
    content_length_header = request.headers.get("content-length")
    if content_length_header is not None:
        try:
            content_length = int(content_length_header)
            if content_length > settings.payment_webhook_max_bytes:
                raise WebhookPayloadTooLargeError(
                    f"Payload length {content_length} exceeds limit of {settings.payment_webhook_max_bytes} bytes"
                )
        except ValueError:
            # Malformed Content-Length header is ignored here and handled via actual body length check
            pass

    # 2. Read raw bytes up to limit + 1 to prevent unbounded memory consumption
    body_chunks: list[bytes] = []
    total_bytes = 0
    max_bytes = settings.payment_webhook_max_bytes

    async for chunk in request.stream():
        total_bytes += len(chunk)
        if total_bytes > max_bytes:
            raise WebhookPayloadTooLargeError(
                f"Payload size exceeds limit of {max_bytes} bytes"
            )
        body_chunks.append(chunk)

    raw_body = b"".join(body_chunks)


    # 3. Process webhook
    service = PaymentWebhookService(db)
    return await service.process_webhook(
        raw_body=raw_body,
        signature_header=x_webhook_signature,
        background_tasks=background_tasks,
    )
