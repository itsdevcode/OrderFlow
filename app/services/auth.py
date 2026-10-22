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
from app.models.audit_log import AuditAction
from app.services.audit import AuditService

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
        try:
            user = await self.authenticate(email, password)
        except (InvalidCredentialsError, InactiveUserError):
            await AuditService.record_login_failure()
            raise

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
        await self._commit_event(AuditAction.LOGIN_SUCCEEDED, user.id)

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

        # Lock the user before reading any token. A token-row lock alone can
        # deadlock when replay detection revokes another concurrently rotated
        # token belonging to the same user. All refresh/logout paths use this
        # shared lock order and hold it through the audited commit.
        user = await self.user_repository.get_by_id(user_id, for_update=True)
        if user is None:
            raise InvalidTokenError

        rt_record = await self.refresh_token_repository.get_by_jti(jti)
        if rt_record is None:
            raise InvalidTokenError

        if rt_record.user_id != user_id:
            raise InvalidTokenError

        if rt_record.is_revoked:
            await self.refresh_token_repository.revoke_all_for_user(user_id)
            await self._commit_event(AuditAction.TOKEN_REUSE_DETECTED, user_id)
            raise InvalidTokenError

        if not user.is_active:
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
        await self._commit_event(AuditAction.TOKEN_REFRESHED, user.id)

        return TokenResponse(
            access_token=create_access_token(user.id),
            refresh_token=new_refresh_token,
        )

    async def logout(self, refresh_token: str) -> None:
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

        user = await self.user_repository.get_by_id(user_id, for_update=True)
        if user is None:
            raise InvalidTokenError

        rt_record = await self.refresh_token_repository.get_by_jti(jti)
        if rt_record is None:
            raise InvalidTokenError
        
        if rt_record.user_id != user_id:
            raise InvalidTokenError

        if rt_record.is_revoked:
            return
        await self.refresh_token_repository.revoke(jti)
        await self._commit_event(AuditAction.LOGOUT, user_id)

    async def _commit_event(self, action: AuditAction, user_id: int) -> None:
        db = self.user_repository.db
        try:
            await AuditService(db).stage(action, actor_id=user_id, resource_id=user_id)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
