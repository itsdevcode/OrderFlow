from enum import Enum
from typing import Protocol

from app.models.notification import Notification


class DeliveryErrorCategory(str, Enum):
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    TEMPLATE_RENDER_ERROR = "TEMPLATE_RENDER_ERROR"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    SIMULATED_FAILURE = "SIMULATED_FAILURE"
    UNEXPECTED_DELIVERY_ERROR = "UNEXPECTED_DELIVERY_ERROR"


class NotificationDeliveryError(Exception):
    """
    Raised when notification delivery fails.
    Carries a sanitized error category so raw details or credentials are never persisted.
    """
    category: DeliveryErrorCategory

    def __init__(
        self,
        category: DeliveryErrorCategory = DeliveryErrorCategory.UNEXPECTED_DELIVERY_ERROR,
        message: str = "Notification delivery failed",
    ) -> None:
        super().__init__(message)
        self.category = category


def sanitize_delivery_error(exc: Exception) -> str:
    """
    Derive a safe, sanitized error category string.
    Never exposes credentials, bearer tokens, recipient addresses, or raw exception bodies.
    """
    if isinstance(exc, NotificationDeliveryError):
        return exc.category.value

    exc_str = str(exc).lower()
    if "timeout" in exc_str:
        return DeliveryErrorCategory.NETWORK_TIMEOUT.value
    if "rate" in exc_str or "429" in exc_str:
        return DeliveryErrorCategory.RATE_LIMITED.value
    if "auth" in exc_str or "unauthorized" in exc_str or "forbidden" in exc_str:
        return DeliveryErrorCategory.AUTHENTICATION_ERROR.value
    if "simulated" in exc_str:
        return DeliveryErrorCategory.SIMULATED_FAILURE.value
    if "connect" in exc_str or "refused" in exc_str:
        return DeliveryErrorCategory.CONNECTION_ERROR.value

    return DeliveryErrorCategory.UNEXPECTED_DELIVERY_ERROR.value


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
        _ = notification
        if self.should_fail:
            raise NotificationDeliveryError(
                category=DeliveryErrorCategory.SIMULATED_FAILURE,
                message="Simulated in-app notification delivery failure",
            )
