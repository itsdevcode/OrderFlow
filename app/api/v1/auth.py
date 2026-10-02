from fastapi import APIRouter, HTTPException, status

from app.dependencies.database import DbSession
from app.exceptions.auth import (
    InactiveUserError,
    InvalidCredentialsError,
    InvalidTokenError,
)
from app.schemas.auth import LoginRequest, RefreshRequest, TokenResponse
from app.services.auth import AuthService
from app.dependencies.auth import CurrentUser
from app.schemas.user import UserRead

router = APIRouter(
    prefix="/auth",
    tags=["Auth"],
)


@router.post(
    "/login",
    response_model=TokenResponse,
)
async def login(
    data: LoginRequest,
    db: DbSession,
) -> TokenResponse:
    service = AuthService(db)

    try:
        return await service.login(
            email=data.email,
            password=data.password,
        )

    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        ) from exc

    except InactiveUserError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive",
        ) from exc


@router.post(
    "/refresh",
    response_model=TokenResponse,
)
async def refresh(
    data: RefreshRequest,
    db: DbSession,
) -> TokenResponse:
    service = AuthService(db)

    try:
        return await service.refresh(data.refresh_token)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        ) from exc
    
@router.get(
    "/me",
    response_model=UserRead,
)
async def me(
    current_user: CurrentUser,
) -> UserRead:
    return UserRead.model_validate(current_user)