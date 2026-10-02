import jwt
import uuid
from datetime import UTC, datetime
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_password
from app.exceptions.auth import (
    InactiveUserError,
    InvalidCredentialsError,
    InvalidTokenError,
)
from app.models.user import User
from app.models.refresh_token import RefreshToken
from app.repositories.user import UserRepository
from app.repositories.refresh_token import RefreshTokenRepository
from app.core.jwt import create_access_token, create_refresh_token, decode_token
from app.schemas.auth import TokenResponse

class AuthService:
    user_repository: UserRepository
    refresh_token_repository: RefreshTokenRepository

    def __init__(self, db: AsyncSession) -> None:
        self.user_repository = UserRepository(db)
        self.refresh_token_repository = RefreshTokenRepository(db)
    
    async def login(
        self,
        email: str,
        password: str,
    ) -> TokenResponse:
        user = await self.authenticate(email, password)

        jti = str(uuid.uuid4())
        refresh_token = create_refresh_token(user.id, jti)
        payload = decode_token(refresh_token)
        expires_at = datetime.fromtimestamp(payload["exp"], UTC)

        rt_record = RefreshToken(
            jti=jti,
            user_id=user.id,
            expires_at=expires_at,
        )
        await self.refresh_token_repository.create(rt_record)

        return TokenResponse(
            access_token=create_access_token(user.id),
            refresh_token=refresh_token,
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
            
            jti = payload.get("jti")
            if not isinstance(jti, str):
                raise InvalidTokenError
        except (jwt.InvalidTokenError, ValueError) as exc:
            raise InvalidTokenError from exc

        rt_record = await self.refresh_token_repository.get_by_jti(jti)
        if rt_record is None:
            raise InvalidTokenError

        if rt_record.is_revoked:
            await self.refresh_token_repository.revoke_all_for_user(user_id)
            raise InvalidTokenError

        user = await self.user_repository.get_by_id(user_id)
        if user is None or not user.is_active:
            raise InvalidTokenError

        await self.refresh_token_repository.revoke(jti)

        new_jti = str(uuid.uuid4())
        new_refresh_token = create_refresh_token(user.id, new_jti)
        new_payload = decode_token(new_refresh_token)
        expires_at = datetime.fromtimestamp(new_payload["exp"], UTC)

        new_rt_record = RefreshToken(
            jti=new_jti,
            user_id=user.id,
            expires_at=expires_at,
        )
        await self.refresh_token_repository.create(new_rt_record)

        return TokenResponse(
            access_token=create_access_token(user.id),
            refresh_token=new_refresh_token,
        )

    async def logout(self, refresh_token: str) -> None:
        try:
            payload = decode_token(refresh_token)
            if payload.get("type") != "refresh":
                raise InvalidTokenError
            
            jti = payload.get("jti")
            if not isinstance(jti, str):
                raise InvalidTokenError
        except (jwt.InvalidTokenError, ValueError) as exc:
            raise InvalidTokenError from exc

        await self.refresh_token_repository.revoke(jti)