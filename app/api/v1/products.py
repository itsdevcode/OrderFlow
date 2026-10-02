from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Path, status

from app.core.roles import RoleName
from app.dependencies.auth import CurrentUser
from app.dependencies.database import DbSession
from app.dependencies.permissions import require_roles
from app.exceptions.category import CategoryNotFoundError
from app.exceptions.product import InvalidPriceError, ProductAlreadyExistsError, ProductNotFoundError
from app.schemas.product import ProductCreate, ProductRead, ProductUpdate
from app.services.product import ProductService

router = APIRouter(prefix="/products", tags=["Products"])

PositiveId = Annotated[int, Path(ge=1)]
Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=100)]


@router.post(
    "",
    response_model=ProductRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(RoleName.ADMIN, RoleName.MANAGER))],
)
async def create_product(
    data: ProductCreate,
    db: DbSession,
):
    try:
        return await ProductService(db).create_product(data)
    except ProductAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
    except CategoryNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except InvalidPriceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        )


@router.get("", response_model=list[ProductRead])
async def list_products(
    db: DbSession,
    _current_user: CurrentUser,
    offset: Offset = 0,
    limit: Limit = 20,
    category_id: int | None = None,
    is_active: bool | None = None,
):
    return await ProductService(db).list_products(
        offset=offset,
        limit=limit,
        category_id=category_id,
        is_active=is_active,
    )


@router.get("/{product_id}", response_model=ProductRead)
async def get_product(
    product_id: PositiveId,
    db: DbSession,
    _current_user: CurrentUser,
):
    try:
        return await ProductService(db).get_product(product_id)
    except ProductNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )


@router.patch(
    "/{product_id}",
    response_model=ProductRead,
    dependencies=[Depends(require_roles(RoleName.ADMIN, RoleName.MANAGER))],
)
async def update_product(
    product_id: PositiveId,
    data: ProductUpdate,
    db: DbSession,
):
    try:
        return await ProductService(db).update_product(product_id, data)
    except ProductNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except CategoryNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except ProductAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
    except InvalidPriceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        )


@router.delete(
    "/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_roles(RoleName.ADMIN, RoleName.MANAGER))],
)
async def delete_product(
    product_id: PositiveId,
    db: DbSession,
) -> None:
    try:
        await ProductService(db).delete_product(product_id)
    except ProductNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
