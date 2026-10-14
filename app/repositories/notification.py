from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
    NotificationType,
)


class NotificationRepository:
    db: AsyncSession

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, notification_id: int) -> Notification | None:
        stmt = select(Notification).where(Notification.id == notification_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id_for_update(self, notification_id: int) -> Notification | None:
        stmt = (
            select(Notification)
            .where(Notification.id == notification_id)
            .with_for_update()
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_deduplication_key(self, deduplication_key: str) -> Notification | None:
        stmt = select(Notification).where(Notification.deduplication_key == deduplication_key)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, notification: Notification) -> Notification:
        self.db.add(notification)
        await self.db.flush()
        return notification

    async def create_on_conflict_do_nothing(
        self,
        user_id: int,
        order_id: int | None,
        notification_type: NotificationType,
        channel: NotificationChannel,
        title: str,
        message: str,
        deduplication_key: str,
    ) -> Notification | None:
        """
        Concurrency-safe insert using PostgreSQL INSERT ... ON CONFLICT (deduplication_key) DO NOTHING RETURNING id.
        Flushes and returns the inserted Notification or None if duplicate.
        """
        stmt = (
            pg_insert(Notification)
            .values(
                user_id=user_id,
                order_id=order_id,
                notification_type=notification_type,
                channel=channel,
                title=title,
                message=message,
                deduplication_key=deduplication_key,
                status=NotificationStatus.PENDING,
                attempts=0,
            )
            .on_conflict_do_nothing(index_elements=["deduplication_key"])
            .returning(Notification)
        )
        result = await self.db.execute(stmt)
        row = result.scalar_one_or_none()
        await self.db.flush()
        return row

    async def list_by_user_id(
        self,
        user_id: int,
        offset: int = 0,
        limit: int = 20,
        status: NotificationStatus | None = None,
        notification_type: NotificationType | None = None,
    ) -> Sequence[Notification]:
        stmt = (
            select(Notification)
            .where(Notification.user_id == user_id)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .offset(offset)
            .limit(limit)
        )
        if status is not None:
            stmt = stmt.where(Notification.status == status)
        if notification_type is not None:
            stmt = stmt.where(Notification.notification_type == notification_type)

        result = await self.db.execute(stmt)
        return result.scalars().all()
