from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query, Path, status
from app.core.roles import RoleName
from app.dependencies.auth import CurrentUser
from app.dependencies.database import DbSession
from app.dependencies.permissions import require_roles
from app.exceptions.warehouse import WarehouseAlreadyExistsError, WarehouseNotFoundError, WarehouseInUseError
from app.schemas.warehouse import WarehouseCreate, WarehouseResponse, WarehouseUpdate
from app.services.warehouse import WarehouseService

router = APIRouter(prefix="/warehouses", tags=["Warehouses"])

PositiveId = Annotated[int, Path(ge=1)]
Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=100)]

@router.post(
    "",
    response_model=WarehouseResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(RoleName.ADMIN, RoleName.MANAGER))],
)
async def create_warehouse(data: WarehouseCreate, db: DbSession):
    try:
        return await WarehouseService(db).create_warehouse(data)
    except WarehouseAlreadyExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

@router.get("", response_model=list[WarehouseResponse])
async def list_warehouses(
    db: DbSession,
    _current_user: CurrentUser,
    offset: Offset = 0,
    limit: Limit = 20,
):
    return await WarehouseService(db).list_warehouses(offset=offset, limit=limit)

@router.get("/{warehouse_id}", response_model=WarehouseResponse)
async def get_warehouse(
    warehouse_id: PositiveId,
    db: DbSession,
    _current_user: CurrentUser,
):
    try:
        return await WarehouseService(db).get_warehouse(warehouse_id)
    except WarehouseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

@router.patch(
    "/{warehouse_id}",
    response_model=WarehouseResponse,
    dependencies=[Depends(require_roles(RoleName.ADMIN, RoleName.MANAGER))],
)
async def update_warehouse(
    warehouse_id: PositiveId,
    data: WarehouseUpdate,
    db: DbSession,
):
    try:
        return await WarehouseService(db).update_warehouse(warehouse_id, data)
    except WarehouseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except WarehouseAlreadyExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

@router.delete(
    "/{warehouse_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_roles(RoleName.ADMIN, RoleName.MANAGER))],
)
async def delete_warehouse(
    warehouse_id: PositiveId,
    db: DbSession,
) -> None:
    try:
        await WarehouseService(db).delete_warehouse(warehouse_id)
    except WarehouseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except WarehouseInUseError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
