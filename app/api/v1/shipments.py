from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.roles import RoleName
from app.db.session import get_db
from app.dependencies.auth import CurrentUser
from app.dependencies.permissions import require_roles
from app.models.shipment import ShipmentStatus
from app.models.user import User
from app.schemas.shipment import (
    ShipmentCreate,
    ShipmentRead,
    ShipmentStatusUpdate,
)
from app.services.shipment import ShipmentService

router = APIRouter(tags=["Shipments"])


@router.post(
    "/orders/{order_id}/shipments",
    response_model=ShipmentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(RoleName.ADMIN, RoleName.MANAGER))],
)
async def create_shipment(
    order_id: int,
    request: ShipmentCreate | None = None,
    current_user: User = Depends(require_roles(RoleName.ADMIN, RoleName.MANAGER)),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """
    Create a shipment for a confirmed, successfully paid order.
    ADMIN / MANAGER only.
    """
    carrier = request.carrier if request else None
    tracking_number = request.tracking_number if request else None

    service = ShipmentService(db)
    return await service.create_shipment(
        order_id=order_id,
        user=current_user,
        carrier=carrier,
        tracking_number=tracking_number,
    )


@router.get(
    "/orders/{order_id}/shipments",
    response_model=ShipmentRead,
    status_code=status.HTTP_200_OK,
)
async def get_order_shipment(
    order_id: int,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """
    Get shipment for a specific order.
    ADMIN / MANAGER: allowed.
    CUSTOMER: allowed only for owned orders (non-owned returns 404).
    """
    service = ShipmentService(db)
    return await service.get_shipment_by_order_id(
        order_id=order_id,
        user=current_user,
    )


@router.get(
    "/shipments/{shipment_id}",
    response_model=ShipmentRead,
    status_code=status.HTTP_200_OK,
)
async def get_shipment(
    shipment_id: int,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """
    Get a specific shipment by ID.
    ADMIN / MANAGER: allowed.
    CUSTOMER: allowed only when shipment belongs to their order (non-owned returns 404).
    """
    service = ShipmentService(db)
    return await service.get_shipment_by_id(
        shipment_id=shipment_id,
        user=current_user,
    )


@router.patch(
    "/shipments/{shipment_id}/status",
    response_model=ShipmentRead,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_roles(RoleName.ADMIN, RoleName.MANAGER))],
)
async def update_shipment_status(
    shipment_id: int,
    request: ShipmentStatusUpdate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """
    Update shipment status.
    ADMIN / MANAGER only.
    """
    service = ShipmentService(db)
    return await service.transition_shipment_status(
        shipment_id=shipment_id,
        update_data=request,
        background_tasks=background_tasks,
    )


@router.get(
    "/shipments",
    response_model=list[ShipmentRead],
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_roles(RoleName.ADMIN, RoleName.MANAGER))],
)
async def list_shipments(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    status: ShipmentStatus | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> Sequence[Any]:
    """
    List shipments with pagination and optional status filter.
    ADMIN / MANAGER only.
    """
    service = ShipmentService(db)
    return await service.list_shipments(
        offset=offset,
        limit=limit,
        status=status,
    )
