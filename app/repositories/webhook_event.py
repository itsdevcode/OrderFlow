from collections.abc import Sequence
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.webhook_event import WebhookEvent


class WebhookEventRepository:
    db: AsyncSession

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, event_id: int) -> WebhookEvent | None:
        stmt = select(WebhookEvent).where(WebhookEvent.id == event_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_provider_and_event_id(
        self,
        provider: str,
        provider_event_id: str,
    ) -> WebhookEvent | None:
        stmt = select(WebhookEvent).where(
            WebhookEvent.provider == provider,
            WebhookEvent.provider_event_id == provider_event_id,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_provider_and_event_id_for_update(
        self,
        provider: str,
        provider_event_id: str,
    ) -> WebhookEvent | None:
        stmt = (
            select(WebhookEvent)
            .where(
                WebhookEvent.provider == provider,
                WebhookEvent.provider_event_id == provider_event_id,
            )
            .with_for_update()
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, event: WebhookEvent) -> WebhookEvent:
        self.db.add(event)
        await self.db.flush()
        return event

    async def list_by_provider(
        self,
        provider: str,
        limit: int = 50,
    ) -> Sequence[WebhookEvent]:
        stmt = (
            select(WebhookEvent)
            .where(WebhookEvent.provider == provider)
            .order_by(WebhookEvent.created_at.desc(), WebhookEvent.id.desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return result.scalars().all()
