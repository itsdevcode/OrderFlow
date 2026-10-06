from fastapi import APIRouter, Depends, Query, status, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.auth import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.order import OrderRead
from app.services.order import OrderService
from app.exceptions.cart import ProductNotActiveError
from app.exceptions.product import ProductNotFoundError
from app.exceptions.order import EmptyCartError, OrderNotFoundError

router = APIRouter(prefix="/orders", tags=["Orders"])


from app.exceptions.inventory import InsufficientStockError

@router.post("", response_model=OrderRead, status_code=status.HTTP_201_CREATED)
async def create_order(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrderRead:
    """Create an order from the authenticated user's cart."""
    try:
        return await OrderService(db).create_order_from_cart(current_user.id)
    except EmptyCartError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except ProductNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ProductNotActiveError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except InsufficientStockError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("", response_model=list[OrderRead], status_code=status.HTTP_200_OK)
async def list_orders(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[OrderRead]:
    """Get the authenticated user's orders."""
    return await OrderService(db).list_orders(current_user.id, offset, limit)


@router.get("/{order_id}", response_model=OrderRead, status_code=status.HTTP_200_OK)
async def get_order(
    order_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrderRead:
    """Get a specific order by ID."""
    try:
        return await OrderService(db).get_order_by_id(current_user.id, order_id)
    except OrderNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
