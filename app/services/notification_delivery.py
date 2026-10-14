from typing import Protocol

from app.models.notification import Notification


class NotificationDeliveryError(Exception):
    """Raised when notification delivery fails."""
    pass


class NotificationDelivery(Protocol):
    async def deliver(self, notification: Notification) -> None:
        """Deliver notification via the configured channel."""
        ...


class InAppNotificationDelivery:
    """
    In-app notification delivery adapter.
    For this iteration, in-app notifications are stored in PostgreSQL.
    The adapter allows finalizing processing, logging, or injecting controlled delivery failures.
    """

    should_fail: bool

    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail

    async def deliver(self, notification: Notification) -> None:
        if self.should_fail:
            raise NotificationDeliveryError("Simulated in-app notification delivery failure")
