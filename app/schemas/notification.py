from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from app.models.notification import (
    NotificationChannel,
    NotificationStatus,
    NotificationType,
)


class NotificationRead(BaseModel):
    id: int
    user_id: int
    order_id: int | None = None
    notification_type: NotificationType
    channel: NotificationChannel
    status: NotificationStatus
    title: str
    message: str
    created_at: datetime
    sent_at: datetime | None = None

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)
