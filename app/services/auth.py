from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_password
from app.exceptions.auth import (
    InactiveUserError,
    InvalidCredentialsError,
)
from app.models.user import User
from app.repositories.user import UserRepository
from app.core.jwt import create_access_token, create_refresh_token
from app.schemas.auth import TokenResponse

class AuthService:
    user_repository: UserRepository

    def __init__(self, db: AsyncSession) -> None:
        self.user_repository = UserRepository(db)
    
    async def login(
        self,
        email: str,
        password: str,
    ) -> TokenResponse:
        user = await self.authenticate(email, password)

        return TokenResponse(
            access_token=create_access_token(user.id),
            refresh_token=create_refresh_token(user.id),
        )
    
    async def authenticate(
        self,
        email: str,
        password: str,
    ) -> User:
        user = await self.user_repository.get_by_email(email)

        if user is None:
            raise InvalidCredentialsError

        if not verify_password(password, user.hashed_password):
            raise InvalidCredentialsError

        if not user.is_active:
            raise InactiveUserError

        return user