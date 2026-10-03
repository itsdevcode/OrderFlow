from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query, Path, status
from app.core.roles import RoleName
from app.dependencies.auth import CurrentUser
from app.dependencies.database import DbSession
from app.dependencies.permissions import require_roles
from app.exceptions.inventory import InventoryNotFoundError, InsufficientStockError
from app.schemas.inventory import InventoryAdjustment, InventoryResponse
from app.services.inventory import InventoryService

router = APIRouter(prefix="/inventory", tags=["Inventory"])

PositiveId = Annotated[int, Path(ge=1)]
Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=100)]

@router.get(
    "/products/{product_id}",
    response_model=list[InventoryResponse],
)
async def list_product_inventory(
    product_id: PositiveId,
    db: DbSession,
    _current_user: CurrentUser,
    offset: Offset = 0,
    limit: Limit = 20,
):
    return await InventoryService(db).list_product_inventory(product_id, offset=offset, limit=limit)

@router.post(
    "/products/{product_id}/warehouses/{warehouse_id}/adjust",
    response_model=InventoryResponse,
    dependencies=[Depends(require_roles(RoleName.ADMIN, RoleName.MANAGER))],
)
async def adjust_stock(
    product_id: PositiveId,
    warehouse_id: PositiveId,
    adjustment: InventoryAdjustment,
    db: DbSession,
):
    try:
        return await InventoryService(db).adjust_stock(product_id, warehouse_id, adjustment)
    except InsufficientStockError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except InventoryNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

@router.get(
    "/products/{product_id}/warehouses/{warehouse_id}",
    response_model=InventoryResponse,
)
async def get_inventory(
    product_id: PositiveId,
    warehouse_id: PositiveId,
    db: DbSession,
    _current_user: CurrentUser,
):
    try:
        return await InventoryService(db).get_inventory(product_id, warehouse_id)
    except InventoryNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
