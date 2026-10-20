from datetime import datetime, timedelta, timezone
from sqlalchemy import func, or_, select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cache_invalidation import CachePendingInvalidation


class CacheInvalidationRepository:
    """
    Persistence helper and repository owning all pending cache invalidation queries.
    Encapsulates staging, worker claiming with SKIP LOCKED, replay, and acknowledgement.
    Does NOT call commit inside helper methods to preserve caller transaction boundaries.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def stage(
        self,
        *,
        key: str | None = None,
        namespace: str | None = None,
    ) -> CachePendingInvalidation:
        """
        Stages a durable cache-invalidation intent in PostgreSQL.
        Flushes to populate entity ID without committing the business transaction.
        """
        event = CachePendingInvalidation(key=key, namespace=namespace)
        self.db.add(event)
        await self.db.flush()
        return event

    async def has_pending(self) -> bool:
        """
        Checks if any unresolved pending or in-flight invalidations exist.
        """
        stmt = select(func.count(CachePendingInvalidation.id))
        result = await self.db.execute(stmt)
        count = result.scalar_one_or_none() or 0
        return count > 0

    async def claim_batch(
        self,
        *,
        claim_token: str,
        lease_seconds: int = 15,
        limit: int = 100,
    ) -> list[CachePendingInvalidation]:
        """
        Selects and claims a batch of pending invalidation rows where:
        1. claimed_at is NULL (newly staged), OR
        2. claimed_at < cutoff (lease expired from crashed worker).
        Uses SELECT ... FOR UPDATE SKIP LOCKED to prevent blocking concurrent workers.
        Updates claimed_at and claim_token, flushes to session. Caller commits.
        """
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=lease_seconds)

        stmt = (
            select(CachePendingInvalidation)
            .where(
                or_(
                    CachePendingInvalidation.claimed_at.is_(None),
                    CachePendingInvalidation.claimed_at < cutoff,
                )
            )
            .order_by(CachePendingInvalidation.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self.db.execute(stmt)
        rows = list(result.scalars().all())
        if not rows:
            return []

        row_ids = [r.id for r in rows]
        await self.db.execute(
            update(CachePendingInvalidation)
            .where(CachePendingInvalidation.id.in_(row_ids))
            .values(
                claimed_at=now,
                claim_token=claim_token,
            )
        )
        await self.db.flush()
        return rows

    async def release_claim(
        self,
        row_ids: list[int],
        *,
        claim_token: str | None = None,
    ) -> None:
        """
        Releases claimed invalidations back to the pool (resets claimed_at and claim_token)
        when Redis operations fail or timeout, enabling fast recovery.
        """
        if not row_ids:
            return
        stmt = (
            update(CachePendingInvalidation)
            .where(CachePendingInvalidation.id.in_(row_ids))
            .values(
                claimed_at=None,
                claim_token=None,
            )
        )
        if claim_token is not None:
            stmt = stmt.where(CachePendingInvalidation.claim_token == claim_token)
        await self.db.execute(stmt)
        await self.db.flush()

    async def acknowledge_batch(
        self,
        row_ids: list[int],
        *,
        claim_token: str | None = None,
    ) -> int:
        """
        Acknowledges and deletes processed invalidation records from PostgreSQL
        only after successful Redis operations.
        """
        if not row_ids:
            return 0
        stmt = delete(CachePendingInvalidation).where(
            CachePendingInvalidation.id.in_(row_ids)
        )
        if claim_token is not None:
            stmt = stmt.where(CachePendingInvalidation.claim_token == claim_token)
        result = await self.db.execute(stmt.returning(CachePendingInvalidation.id))
        await self.db.flush()
        return len(result.scalars().all())
