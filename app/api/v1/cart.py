from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, status

from app.dependencies.auth import CurrentUser
from app.dependencies.database import DbSession
from app.exceptions.cart import CartItemNotFoundError, InvalidQuantityError, ProductNotActiveError
from app.exceptions.product import ProductNotFoundError
from app.schemas.cart import CartItemAdd, CartItemUpdate, CartRead
from app.services.cart import CartService

router = APIRouter(prefix="/cart", tags=["Cart"])

PositiveId = Annotated[int, Path(ge=1)]


@router.get("", response_model=CartRead)
async def get_cart(
    db: DbSession,
    current_user: CurrentUser,
):
    return await CartService(db).get_or_create_cart(current_user.id)


@router.post("/items", response_model=CartRead)
async def add_item_to_cart(
    data: CartItemAdd,
    db: DbSession,
    current_user: CurrentUser,
):
    try:
        return await CartService(db).add_item(current_user.id, data)
    except ProductNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except ProductNotActiveError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except InvalidQuantityError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        )


@router.patch("/items/{item_id}", response_model=CartRead)
async def update_cart_item(
    item_id: PositiveId,
    data: CartItemUpdate,
    db: DbSession,
    current_user: CurrentUser,
):
    try:
        return await CartService(db).update_item(current_user.id, item_id, data)
    except CartItemNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except InvalidQuantityError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        )


@router.delete("/items/{item_id}", response_model=CartRead)
async def remove_cart_item(
    item_id: PositiveId,
    db: DbSession,
    current_user: CurrentUser,
):
    try:
        return await CartService(db).remove_item(current_user.id, item_id)
    except CartItemNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def clear_cart(
    db: DbSession,
    current_user: CurrentUser,
) -> None:
    _ = await CartService(db).clear_cart(current_user.id)
