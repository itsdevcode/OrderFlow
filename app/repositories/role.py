from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.role import Role


class RoleRepository:
    db: AsyncSession
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, role_id: int) -> Role | None:
        result = await self.db.execute(
            select(Role).where(Role.id == role_id)
        )
        return result.scalar_one_or_none()

    async def get_by_name(self, name: str) -> Role | None:
        result = await self.db.execute(
            select(Role).where(Role.name == name)
        )
        return result.scalar_one_or_none()

    async def list(self) -> list[Role]:
        result = await self.db.execute(
            select(Role).order_by(Role.id)
        )
        return list(result.scalars().all())

    async def create(self, *, name: str) -> Role:
        role = Role(name=name)

        self.db.add(role)

        await self.db.flush()
        await self.db.refresh(role)

        return role