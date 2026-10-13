from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.exceptions.payment import PaymentNotFoundError
from app.exceptions.webhook import (
    WebhookAuthenticationError,
    WebhookEventConflictError,
    WebhookPayloadError,
    WebhookReconciliationRequiredError,
)
from app.models.inventory_reservation import ReservationStatus
from app.models.order import Order, OrderStatus
from app.models.payment import Payment, PaymentStatus
from app.models.webhook_event import WebhookEvent, WebhookEventStatus
from app.repositories.inventory import InventoryRepository
from app.repositories.inventory_reservation import InventoryReservationRepository
from app.repositories.payment import PaymentRepository
from app.repositories.webhook_event import WebhookEventRepository
from app.schemas.webhook import PaymentWebhookPayload, WebhookResponse
from app.services.inventory_reservation import InventoryReservationService
from app.services.payment import PaymentService

logger = logging.getLogger(__name__)


def compute_webhook_signature(payload_bytes: bytes, secret: str) -> str:
    """
    Compute HMAC-SHA256 hex digest for payload using webhook secret.
    """
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


def verify_webhook_signature(payload_bytes: bytes, signature_header: str | None, secret: str) -> None:
    """
    Verify webhook signature against secret using constant-time comparison.
    Raises WebhookAuthenticationError on missing or invalid signature.
    Never exposes the webhook secret in logs or error messages.
    """
    if not signature_header:
        raise WebhookAuthenticationError("Missing webhook signature header")

    expected_sig = compute_webhook_signature(payload_bytes, secret)
    if not hmac.compare_digest(expected_sig, signature_header.strip()):
        raise WebhookAuthenticationError("Invalid webhook signature")


class PaymentWebhookService:
    """
    Handles payment webhooks securely and idempotently under strict database transactions.
    
    Lock-ordering convention (to avoid deadlocks with concurrent transactions):
    1. Order (orders row locked with FOR UPDATE)
    2. Payment (payments row locked with FOR UPDATE)
    3. Inventory Reservations (inventory_reservations locked in deterministic order: product_id ASC, warehouse_id ASC, id ASC)
    4. Inventories (inventories locked in deterministic order: product_id ASC, warehouse_id ASC)
    """

    db: AsyncSession
    webhook_repo: WebhookEventRepository
    payment_repo: PaymentRepository
    reservation_repo: InventoryReservationRepository
    inventory_repo: InventoryRepository
    payment_service: PaymentService
    reservation_service: InventoryReservationService

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.webhook_repo = WebhookEventRepository(db)
        self.payment_repo = PaymentRepository(db)
        self.reservation_repo = InventoryReservationRepository(db)
        self.inventory_repo = InventoryRepository(db)
        self.payment_service = PaymentService(db)
        self.reservation_service = InventoryReservationService(db)

    def parse_payload(self, raw_body: bytes) -> PaymentWebhookPayload:
        """
        Parse raw bytes into validated PaymentWebhookPayload schema.
        Raises WebhookPayloadError if JSON is malformed or invalid.
        """
        try:
            data = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            raise WebhookPayloadError("Invalid JSON body") from exc

        try:
            return PaymentWebhookPayload.model_validate(data)
        except Exception as exc:
            raise WebhookPayloadError(f"Invalid webhook payload structure: {exc}") from exc

    async def process_webhook(
        self,
        raw_body: bytes,
        signature_header: str | None,
        secret: str | None = None,
        max_timestamp_skew_seconds: int = 300,
    ) -> WebhookResponse:
        """
        Entrypoint for payment webhook processing.
        Atomic, caller-owned transaction across WebhookEvent, Order, Payment, InventoryReservation, Inventory.
        """
        webhook_secret = secret or settings.payment_webhook_secret

        # 1. Verify HMAC-SHA256 signature
        verify_webhook_signature(raw_body, signature_header, webhook_secret)

        # 2. Parse & validate payload
        payload = self.parse_payload(raw_body)

        # 3. Timestamp tolerance check (replay window)
        now_ts = int(datetime.now(timezone.utc).timestamp())
        if abs(now_ts - payload.timestamp) > max_timestamp_skew_seconds:
            raise WebhookPayloadError(
                f"Webhook timestamp outside allowable window (diff: {abs(now_ts - payload.timestamp)}s, max: {max_timestamp_skew_seconds}s)"
            )

        payload_hash = hashlib.sha256(raw_body).hexdigest()
        provider = payload.provider
        provider_event_id = payload.event_id

        # 4. Check for existing WebhookEvent (fast path / idempotency)
        existing_event = await self.webhook_repo.get_by_provider_and_event_id(provider, provider_event_id)
        if existing_event:
            # Check payload consistency: contradictory payload reuse is rejected
            if existing_event.payload_hash and existing_event.payload_hash != payload_hash:
                raise WebhookEventConflictError(
                    f"Event ID '{provider_event_id}' was already received with a different payload"
                )
            # If already processed, acknowledge idempotently without re-applying side effects
            if existing_event.status == WebhookEventStatus.PROCESSED:
                return WebhookResponse(
                    status="success",
                    event_id=provider_event_id,
                    message="Event already processed",
                )

        # 5. Execute processing within caller-owned atomic transaction
        try:
            return await self._execute_webhook_transaction(payload, payload_hash)
        except IntegrityError:
            await self.db.rollback()
            # Handle concurrent race condition on (provider, provider_event_id) unique constraint
            existing = await self.webhook_repo.get_by_provider_and_event_id(provider, provider_event_id)
            if existing:
                if existing.payload_hash and existing.payload_hash != payload_hash:
                    raise WebhookEventConflictError(
                        f"Event ID '{provider_event_id}' was already received with a different payload"
                    )
                if existing.status == WebhookEventStatus.PROCESSED:
                    return WebhookResponse(
                        status="success",
                        event_id=provider_event_id,
                        message="Event already processed",
                    )
            raise

    async def _execute_webhook_transaction(
        self,
        payload: PaymentWebhookPayload,
        payload_hash: str,
    ) -> WebhookResponse:
        provider = payload.provider
        provider_event_id = payload.event_id

        # Lock or insert WebhookEvent
        existing_event = await self.webhook_repo.get_by_provider_and_event_id_for_update(
            provider, provider_event_id
        )
        if existing_event:
            if existing_event.payload_hash and existing_event.payload_hash != payload_hash:
                raise WebhookEventConflictError(
                    f"Event ID '{provider_event_id}' was already received with a different payload"
                )
            if existing_event.status == WebhookEventStatus.PROCESSED:
                await self.db.rollback()
                return WebhookResponse(
                    status="success",
                    event_id=provider_event_id,
                    message="Event already processed",
                )
            webhook_event = existing_event
        else:
            webhook_event = WebhookEvent(
                provider=provider,
                provider_event_id=provider_event_id,
                event_type=payload.event_type,
                status=WebhookEventStatus.RECEIVED,
                payload_hash=payload_hash,
            )
            self.db.add(webhook_event)
            await self.db.flush()

        # Step A: Resolve payment row using provider and provider_payment_id
        payment = await self.payment_repo.get_by_provider_payment_id(
            provider=provider,
            provider_payment_id=payload.provider_payment_id,
        )
        if not payment:
            # Cannot find payment: quarantine event as FAILED
            webhook_event.status = WebhookEventStatus.FAILED
            webhook_event.error_message = f"Payment not found for provider_payment_id '{payload.provider_payment_id}'"
            webhook_event.processed_at = datetime.now(timezone.utc)
            await self.db.commit()
            raise PaymentNotFoundError(
                f"No payment record found for provider '{provider}' and id '{payload.provider_payment_id}'"
            )

        webhook_event.payment_id = payment.id

        # Step B: Validate amount and currency against persisted Payment
        if payment.amount != payload.amount:
            webhook_event.status = WebhookEventStatus.FAILED
            webhook_event.error_message = f"Amount mismatch: payload={payload.amount}, db={payment.amount}"
            webhook_event.processed_at = datetime.now(timezone.utc)
            await self.db.commit()
            raise WebhookPayloadError(
                f"Payment amount mismatch: expected {payment.amount}, received {payload.amount}"
            )

        if payment.currency != payload.currency:
            webhook_event.status = WebhookEventStatus.FAILED
            webhook_event.error_message = f"Currency mismatch: payload={payload.currency}, db={payment.currency}"
            webhook_event.processed_at = datetime.now(timezone.utc)
            await self.db.commit()
            raise WebhookPayloadError(
                f"Payment currency mismatch: expected {payment.currency}, received {payload.currency}"
            )

        # Step C: Acquire locks according to strict hierarchy:
        # 1. Order row lock
        order_stmt = select(Order).where(Order.id == payment.order_id).with_for_update()
        order_res = await self.db.execute(order_stmt)
        order = order_res.scalar_one_or_none()
        if not order:
            webhook_event.status = WebhookEventStatus.FAILED
            webhook_event.error_message = f"Order {payment.order_id} not found"
            webhook_event.processed_at = datetime.now(timezone.utc)
            await self.db.commit()
            raise WebhookPayloadError(f"Order {payment.order_id} not found")

        # 2. Payment row lock
        payment_locked = await self.payment_repo.get_by_id_for_update(payment.id)
        if not payment_locked:
            webhook_event.status = WebhookEventStatus.FAILED
            webhook_event.error_message = f"Payment {payment.id} missing during lock"
            webhook_event.processed_at = datetime.now(timezone.utc)
            await self.db.commit()
            raise PaymentNotFoundError(f"Payment {payment.id} not found")

        # Handle event types
        if payload.event_type == "payment.succeeded":
            await self._handle_payment_succeeded(order, payment_locked, webhook_event)
        elif payload.event_type == "payment.failed":
            await self._handle_payment_failed(order, payment_locked, webhook_event)
        else:
            webhook_event.status = WebhookEventStatus.FAILED
            webhook_event.error_message = f"Unsupported event type: {payload.event_type}"
            webhook_event.processed_at = datetime.now(timezone.utc)
            await self.db.commit()
            raise WebhookPayloadError(f"Unsupported event type: {payload.event_type}")

        # Mark WebhookEvent PROCESSED
        webhook_event.status = WebhookEventStatus.PROCESSED
        webhook_event.processed_at = datetime.now(timezone.utc)
        webhook_event.error_message = None

        # Atomically commit all business changes
        await self.db.commit()

        return WebhookResponse(
            status="success",
            event_id=provider_event_id,
            message="Webhook processed successfully",
        )

    async def _handle_payment_succeeded(
        self,
        order: Order,
        payment: Payment,
        webhook_event: WebhookEvent,
    ) -> None:
        """
        Handle payment.succeeded event:
        - If payment is already SUCCEEDED and order is CONFIRMED, this is idempotent.
        - Verify order is not CANCELLED.
        - Check reservations are ACTIVE and not expired.
        - Transition payment to SUCCEEDED.
        - Confirm inventory reservations (reduces reserved_quantity only, available_quantity untouched).
        - Transition order to CONFIRMED.
        """
        # Idempotency check: if already SUCCEEDED
        if payment.status == PaymentStatus.SUCCEEDED:
            if order.status == OrderStatus.CONFIRMED:
                return
            if order.status == OrderStatus.CANCELLED:
                raise WebhookPayloadError("Cannot process success for already cancelled order")

        # If payment is in FAILED or REFUNDED, cannot transition to SUCCEEDED
        if payment.status in (PaymentStatus.FAILED, PaymentStatus.REFUNDED):
            raise WebhookPayloadError(
                f"Conflicting payment state: payment is already {payment.status.value}, cannot transition to SUCCEEDED"
            )

        if order.status == OrderStatus.CANCELLED:
            raise WebhookPayloadError("Order has already been cancelled")

        # Fetch and lock all inventory reservations for this order
        reservations = await self.reservation_repo.list_by_order_id(order.id, for_update=True)
        if not reservations:
            # Orders must have reservations
            raise WebhookReconciliationRequiredError(
                f"No reservations found for order {order.id}. Quarantined for reconciliation."
            )

        now = datetime.now(timezone.utc)
        for res in reservations:
            if res.status != ReservationStatus.ACTIVE or res.expires_at <= now:
                # Quarantined for reconciliation rather than losing evidence of genuine payment
                raise WebhookReconciliationRequiredError(
                    f"Reservation {res.id} for order {order.id} is {res.status.value} (expires_at={res.expires_at}). Cannot confirm order. Quarantined for reconciliation."
                )

        # Transition payment to SUCCEEDED (caller-owned, no commit)
        _ = await self.payment_service.apply_payment_transition(payment, PaymentStatus.SUCCEEDED)

        # Confirm reservations in deterministic order
        for res in reservations:
            # confirm_reservation uses transition_reservation which acquires inventory for_update and decrements reserved_quantity
            _ = await self.reservation_service.confirm_reservation(res.id)

        # Transition order to CONFIRMED
        order.status = OrderStatus.CONFIRMED
        await self.db.flush()

    async def _handle_payment_failed(
        self,
        order: Order,
        payment: Payment,
        webhook_event: WebhookEvent,
    ) -> None:
        """
        Handle payment.failed event:
        - If payment is already FAILED, idempotent.
        - If payment is already SUCCEEDED, reject conflict.
        - Transition eligible PENDING payment to FAILED.
        - Do NOT cancel the order or release reservations automatically: customer is allowed to retry payment
          while order reservations remain ACTIVE until reservation expiry or customer cancellation.
        """
        if payment.status == PaymentStatus.FAILED:
            return

        if payment.status == PaymentStatus.SUCCEEDED:
            raise WebhookPayloadError("Conflicting event: payment is already marked SUCCEEDED")

        # Transition payment to FAILED (caller-owned, no commit)
        _ = await self.payment_service.apply_payment_transition(payment, PaymentStatus.FAILED)
        await self.db.flush()
