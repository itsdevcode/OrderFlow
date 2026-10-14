from collections.abc import Sequence
from datetime import datetime

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

    async def claim_for_dispatch(
        self,
        notification_id: int,
        claim_token: str,
        now: datetime,
        max_attempts: int,
        lease_seconds: int,
    ) -> Notification | None:
        """
        Atomically lock and claim a notification for dispatch if:
        1. status is PENDING, OR
        2. status is FAILED and attempts < max_attempts, OR
        3. status is PROCESSING but claimed_at is older than lease_seconds (stranded recovery).
        Transitions status -> PROCESSING, records claim_token and claimed_at, increments attempts.
        Returns the claimed Notification entity, or None if not claimable.
        """
        stmt = (
            select(Notification)
            .where(Notification.id == notification_id)
            .with_for_update()
        )
        result = await self.db.execute(stmt)
        notification = result.scalar_one_or_none()
        if not notification:
            return None

        # Cannot claim already SENT notifications
        if notification.status == NotificationStatus.SENT:
            return None

        # Check claim eligibility
        is_pending = notification.status == NotificationStatus.PENDING
        is_retryable_failed = (
            notification.status == NotificationStatus.FAILED
            and notification.attempts < max_attempts
        )
        is_stranded_processing = False
        if notification.status == NotificationStatus.PROCESSING:
            if notification.claimed_at is None:
                is_stranded_processing = True
            else:
                elapsed = (now - notification.claimed_at).total_seconds()
                if elapsed >= lease_seconds:
                    is_stranded_processing = True

        if not (is_pending or is_retryable_failed or is_stranded_processing):
            return None

        # Enforce max attempts
        if notification.attempts >= max_attempts:
            return None

        # Claim notification atomically
        notification.status = NotificationStatus.PROCESSING
        notification.claim_token = claim_token
        notification.claimed_at = now
        notification.attempts += 1
        await self.db.flush()
        return notification

    async def finalize_dispatch(
        self,
        notification_id: int,
        claim_token: str,
        success: bool,
        now: datetime,
        sanitized_error: str | None = None,
    ) -> bool:
        """
        Fenced finalization: updates status to SENT or FAILED only if the claim_token matches.
        Prevents stale workers whose lease expired from overwriting newer outcomes.
        Returns True if finalized, False if claim was lost/stale.
        """
        stmt = (
            select(Notification)
            .where(Notification.id == notification_id)
            .with_for_update()
        )
        result = await self.db.execute(stmt)
        notification = result.scalar_one_or_none()
        if not notification:
            return False

        # Fencing check: token must match current claim
        if notification.claim_token != claim_token:
            return False

        if success:
            notification.status = NotificationStatus.SENT
            notification.sent_at = now
            notification.error_message = None
        else:
            notification.status = NotificationStatus.FAILED
            notification.error_message = sanitized_error

        # Release claim token
        notification.claim_token = None
        notification.claimed_at = None
        await self.db.flush()
        return True
