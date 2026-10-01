from fastapi import APIRouter, HTTPException, status

from app.dependencies.database import DbSession
from app.exceptions.user import RoleNotFoundError, UserAlreadyExistsError
from app.schemas.user import UserCreate, UserRead
from app.services.user import UserService


router = APIRouter(
    prefix="/users",
    tags=["Users"],
)


@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_user(
    data: UserCreate,
    db: DbSession,
) -> UserRead:
    service = UserService(db)

    try:
        user = await service.create(data)

    except UserAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User with this email already exists",
        ) from exc

    except RoleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Role not found",
        ) from exc

    return UserRead.model_validate(user)