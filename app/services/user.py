from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.exceptions.user import RoleNotFoundError, UserAlreadyExistsError
from app.models.user import User
from app.repositories.role import RoleRepository
from app.repositories.user import UserRepository
from app.schemas.user import UserCreate


class UserService:
    db: AsyncSession
    user_repository: UserRepository
    role_repository: RoleRepository

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.user_repository = UserRepository(db)
        self.role_repository = RoleRepository(db)

    async def create(self, data: UserCreate) -> User:
        existing_user = await self.user_repository.get_by_email(
            str(data.email)
        )

        if existing_user is not None:
            raise UserAlreadyExistsError

        role = await self.role_repository.get_by_id(data.role_id)

        if role is None:
            raise RoleNotFoundError

        hashed_password = hash_password(data.password)

        user = await self.user_repository.create(
            name=data.name,
            email=str(data.email),
            hashed_password=hashed_password,
            role_id=data.role_id,
        )

        await self.db.commit()
        await self.db.refresh(user)

        return user