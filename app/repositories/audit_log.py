from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog


class AuditLogRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def stage(self, entry: AuditLog) -> AuditLog:
        self.db.add(entry)
        await self.db.flush()
        return entry

    async def list_for_resource(
        self, resource_type: str, resource_id: str, *, offset: int = 0, limit: int = 50,
    ) -> list[AuditLog]:
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("Invalid audit pagination")
        result = await self.db.execute(
            select(AuditLog).where(AuditLog.resource_type == resource_type, AuditLog.resource_id == resource_id)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).offset(offset).limit(limit)
        )
        return list(result.scalars())
