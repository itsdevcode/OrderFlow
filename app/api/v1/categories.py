from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Path, status

from app.core.roles import RoleName
from app.dependencies.auth import CurrentUser
from app.dependencies.database import DbSession
from app.dependencies.permissions import require_roles
from app.exceptions.category import CategoryAlreadyExistsError, CategoryNotFoundError, CategoryHasProductsError
from app.schemas.category import CategoryCreate, CategoryResponse, CategoryUpdate
from app.services.category import CategoryService

router = APIRouter(prefix="/categories", tags=["Categories"])

PositiveId = Annotated[int, Path(ge=1)]
Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=100)]


@router.post(
    "",
    response_model=CategoryResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(RoleName.ADMIN, RoleName.MANAGER))],
)
async def create_category(
    data: CategoryCreate,
    db: DbSession,
):
    try:
        return await CategoryService(db).create_category(data)
    except CategoryAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )


@router.get("", response_model=list[CategoryResponse])
async def list_categories(
    db: DbSession,
    _current_user: CurrentUser,
    offset: Offset = 0,
    limit: Limit = 20,
):
    return await CategoryService(db).list_categories(offset=offset, limit=limit)


@router.get("/{category_id}", response_model=CategoryResponse)
async def get_category(
    category_id: PositiveId,
    db: DbSession,
    _current_user: CurrentUser,
):
    try:
        return await CategoryService(db).get_category(category_id)
    except CategoryNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )


@router.patch(
    "/{category_id}",
    response_model=CategoryResponse,
    dependencies=[Depends(require_roles(RoleName.ADMIN, RoleName.MANAGER))],
)
async def update_category(
    category_id: PositiveId,
    data: CategoryUpdate,
    db: DbSession,
):
    try:
        return await CategoryService(db).update_category(category_id, data)
    except CategoryNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except CategoryAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )


@router.delete(
    "/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_roles(RoleName.ADMIN, RoleName.MANAGER))],
)
async def delete_category(
    category_id: PositiveId,
    db: DbSession,
) -> None:
    try:
        await CategoryService(db).delete_category(category_id)
    except CategoryNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except CategoryHasProductsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
