from collections.abc import Sequence
from datetime import datetime, timezone
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.notification_templates import get_notification_template
from app.exceptions.notification import NotificationNotFoundError
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
    NotificationType,
)
from app.repositories.notification import NotificationRepository
import uuid
from app.core.config import settings
from app.services.notification_delivery import (
    InAppNotificationDelivery,
    NotificationDelivery,
    sanitize_delivery_error,
)

logger = logging.getLogger(__name__)


class NotificationService:
    db: AsyncSession
    notification_repo: NotificationRepository
    delivery_adapter: NotificationDelivery
    max_attempts: int
    lease_seconds: int

    def __init__(
        self,
        db: AsyncSession,
        delivery_adapter: NotificationDelivery | None = None,
        max_attempts: int | None = None,
        lease_seconds: int | None = None,
    ) -> None:
        self.db = db
        self.notification_repo = NotificationRepository(db)
        self.delivery_adapter = delivery_adapter or InAppNotificationDelivery()
        self.max_attempts = (
            max_attempts if max_attempts is not None else settings.notification_max_attempts
        )
        self.lease_seconds = (
            lease_seconds if lease_seconds is not None else settings.notification_claim_lease_seconds
        )

    async def get_by_id(self, notification_id: int, user_id: int) -> Notification:
        """
        Get notification by ID ensuring user ownership isolation.
        Returns 404 if missing or belonging to another user.
        """
        notification = await self.notification_repo.get_by_id(notification_id)
        if not notification or notification.user_id != user_id:
            raise NotificationNotFoundError("Notification not found")
        return notification

    async def list_by_user(
        self,
        user_id: int,
        offset: int = 0,
        limit: int = 20,
        status: NotificationStatus | None = None,
        notification_type: NotificationType | None = None,
    ) -> Sequence[Notification]:
        """
        List notifications for authenticated user with pagination and optional filters.
        """
        return await self.notification_repo.list_by_user_id(
            user_id=user_id,
            offset=offset,
            limit=limit,
            status=status,
            notification_type=notification_type,
        )

    async def create_pending_notification(
        self,
        user_id: int,
        order_id: int | None,
        order_number: str,
        notification_type: NotificationType,
        deduplication_key: str,
        channel: NotificationChannel = NotificationChannel.IN_APP,
    ) -> Notification | None:
        """
        Creates a pending notification within the caller's ongoing database transaction.
        Uses INSERT ... ON CONFLICT DO NOTHING to ensure idempotency.
        Flushes to session without committing so caller's business transaction controls commit.
        Returns the created Notification (or None if already exists / conflict).
        """
        title, message = get_notification_template(notification_type, order_number)
        return await self.notification_repo.create_on_conflict_do_nothing(
            user_id=user_id,
            order_id=order_id,
            notification_type=notification_type,
            channel=channel,
            title=title,
            message=message,
            deduplication_key=deduplication_key,
        )

    async def dispatch_notification(self, notification_id: int) -> None:
        """
        Transaction-safe, bounded, fenced notification dispatch:
        1. Phase 1 (Short Transaction): Atomically claim notification with claim_token,
           increment attempts, enforce max_attempts limit, and commit claim.
        2. Phase 2 (Non-Transactional): Deliver via delivery adapter OUTSIDE database transaction.
        3. Phase 3 (Short Transaction): Finalize outcome (SENT or FAILED) with claim_token fencing
           and sanitized error category recording.

        Recovery & Scheduling Architecture:
        - When a worker claims a notification, it acquires a lease for `notification_claim_lease_seconds`.
        - If the worker crashes or hangs during Phase 2 delivery, the notification remains in PROCESSING
          until the lease expires.
        - Lease-based recovery requires a future scheduler (e.g., cron job / periodic background worker)
          or an explicit redispatch mechanism (e.g., operational retry sweep or re-queue endpoint) to
          query for expired PROCESSING / eligible FAILED notifications and trigger `dispatch_notification`.
        - No heavyweight external queue system (e.g. Celery, RabbitMQ, SQS) is required or introduced
          in this service; PostgreSQL row locks and lease timeouts provide atomic coordination.
        """
        claim_token = uuid.uuid4().hex
        now = datetime.now(timezone.utc)

        # -------------------------------------------------------------
        # Phase 1: Atomically claim the notification in a short transaction
        # -------------------------------------------------------------
        claimed_notification: Notification | None = None
        try:
            claimed_notification = await self.notification_repo.claim_for_dispatch(
                notification_id=notification_id,
                claim_token=claim_token,
                now=now,
                max_attempts=self.max_attempts,
                lease_seconds=self.lease_seconds,
            )
            await self.db.commit()
        except Exception as exc:
            try:
                if self.db.is_active:
                    await self.db.rollback()
            except Exception:
                pass
            logger.exception("Failed to claim notification %s: %s", notification_id, exc)
            return

        if not claimed_notification:
            # Either already SENT, exceeding max_attempts, or claimed by active concurrent worker
            logger.debug(
                "Notification %s not eligible for claim or already processed", notification_id
            )
            return

        # -------------------------------------------------------------
        # Phase 2: Deliver OUTSIDE the database transaction
        # -------------------------------------------------------------
        success = False
        sanitized_error: str | None = None
        try:
            await self.delivery_adapter.deliver(claimed_notification)
            success = True
        except Exception as exc:
            sanitized_error = sanitize_delivery_error(exc)
            logger.error(
                "Delivery failed for notification %s [category=%s]",
                notification_id,
                sanitized_error,
            )

        # -------------------------------------------------------------
        # Phase 3: Finalize outcome in another short transaction with fencing
        # -------------------------------------------------------------
        finalize_now = datetime.now(timezone.utc)
        try:
            finalized = await self.notification_repo.finalize_dispatch(
                notification_id=notification_id,
                claim_token=claim_token,
                success=success,
                now=finalize_now,
                sanitized_error=sanitized_error,
            )
            if not finalized:
                logger.warning(
                    "Fencing check failed for notification %s: token mismatch or lease expired",
                    notification_id,
                )
            await self.db.commit()
        except Exception as exc:
            try:
                if self.db.is_active:
                    await self.db.rollback()
            except Exception:
                pass
            logger.exception(
                "Failed to finalize dispatch for notification %s: %s", notification_id, exc
            )
