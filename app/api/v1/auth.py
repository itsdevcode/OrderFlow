from fastapi import APIRouter, HTTPException, status

from app.dependencies.database import DbSession
from app.exceptions.auth import (
    InactiveUserError,
    InvalidCredentialsError,
)
from app.schemas.auth import LoginRequest, TokenResponse
from app.services.auth import AuthService


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