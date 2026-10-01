from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class UserRepository:
    db: AsyncSession

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, user_id: int) -> User | None:
        result = await self.db.execute(
            select(User).where(User.id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        result = await self.db.execute(
            select(User).where(User.email == email)
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> list[User]:
        result = await self.db.execute(
            select(User)
            .order_by(User.created_at.desc())
            .offset(offset)
            .limit(limit)
        )

        return list(result.scalars().all())

    async def create(
        self,
        *,
        name: str,
        email: str,
        hashed_password: str,
        role_id: int,
    ) -> User:
        user = User(
            name=name,
            email=email,
            hashed_password=hashed_password,
            role_id=role_id,
        )

        self.db.add(user)

        await self.db.flush()
        await self.db.refresh(user)

        return user

    async def update(
        self,
        user: User,
        *,
        name: str | None = None,
        role_id: int | None = None,
        is_active: bool | None = None,
    ) -> User:
        if name is not None:
            user.name = name

        if role_id is not None:
            user.role_id = role_id

        if is_active is not None:
            user.is_active = is_active

        await self.db.flush()
        await self.db.refresh(user)

        return user

    async def delete(self, user: User) -> None:
        await self.db.delete(user)
        await self.db.flush()