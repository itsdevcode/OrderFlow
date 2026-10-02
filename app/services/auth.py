import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_password
from app.exceptions.auth import (
    InactiveUserError,
    InvalidCredentialsError,
    InvalidTokenError,
)
from app.models.user import User
from app.repositories.user import UserRepository
from app.core.jwt import create_access_token, create_refresh_token, decode_token
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

    async def refresh(self, refresh_token: str) -> TokenResponse:
        try:
            payload = decode_token(refresh_token)

            if payload.get("type") != "refresh":
                raise InvalidTokenError

            subject = payload.get("sub")
            if not isinstance(subject, (str, int)):
                raise InvalidTokenError

            user_id = int(subject)
        except (jwt.InvalidTokenError, ValueError) as exc:
            raise InvalidTokenError from exc

        user = await self.user_repository.get_by_id(user_id)

        if user is None or not user.is_active:
            raise InvalidTokenError

        return TokenResponse(
            access_token=create_access_token(user.id),
            refresh_token=create_refresh_token(user.id),
        )