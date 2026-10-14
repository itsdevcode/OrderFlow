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
from app.services.notification_delivery import (
    InAppNotificationDelivery,
    NotificationDelivery,
    NotificationDeliveryError,
)

logger = logging.getLogger(__name__)


class NotificationService:
    db: AsyncSession
    notification_repo: NotificationRepository
    delivery_adapter: NotificationDelivery

    def __init__(
        self,
        db: AsyncSession,
        delivery_adapter: NotificationDelivery | None = None,
    ) -> None:
        self.db = db
        self.notification_repo = NotificationRepository(db)
        self.delivery_adapter = delivery_adapter or InAppNotificationDelivery()

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
        Dispatches a notification:
        1. Atomically locks and claims the notification (PENDING/FAILED -> PROCESSING).
        2. Increments attempts.
        3. Calls delivery adapter.
        4. On success: sets status=SENT, sent_at=now.
        5. On error: logs and sets status=FAILED, error_message=sanitized.
        6. Commits transaction.
        """
        try:
            notification = await self.notification_repo.get_by_id_for_update(notification_id)
            if not notification:
                logger.warning("Notification %s not found for dispatch", notification_id)
                await self.db.rollback()
                return

            # If already SENT, repeated dispatch does nothing
            if notification.status == NotificationStatus.SENT:
                await self.db.rollback()
                return

            # Claim notification: transition to PROCESSING and increment attempts
            notification.status = NotificationStatus.PROCESSING
            notification.attempts += 1
            await self.db.flush()

            # Attempt delivery via adapter
            try:
                await self.delivery_adapter.deliver(notification)
                notification.status = NotificationStatus.SENT
                notification.sent_at = datetime.now(timezone.utc)
                notification.error_message = None
            except (NotificationDeliveryError, Exception) as exc:
                logger.error("Failed to deliver notification %s: %s", notification_id, exc)
                notification.status = NotificationStatus.FAILED
                notification.error_message = str(exc)

            await self.db.commit()

        except Exception as exc:
            try:
                if self.db.is_active:
                    await self.db.rollback()
            except Exception:
                pass
            logger.exception("Unexpected error in dispatch_notification %s: %s", notification_id, exc)
