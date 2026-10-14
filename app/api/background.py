import logging

from app.db.session import AsyncSessionLocal
from app.services.notification import NotificationService
from app.services.notification_delivery import NotificationDelivery

logger = logging.getLogger(__name__)


async def dispatch_notification_task(
    notification_id: int,
    delivery_adapter: NotificationDelivery | None = None,
) -> None:
    """
    Background task to dispatch a single notification using its own fresh AsyncSession.
    Does not reuse request-scoped AsyncSession.
    Passes immutable notification_id, not ORM entities.
    """
    async with AsyncSessionLocal() as session:
        service = NotificationService(session, delivery_adapter=delivery_adapter)
        await service.dispatch_notification(notification_id)
