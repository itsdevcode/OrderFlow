from typing import Any
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.schemas.payment import PaymentInitiateRequest, PaymentRead
from app.services.payment import PaymentService

router = APIRouter(prefix="/orders/{order_id}/payments", tags=["Payments"])


@router.post("", response_model=PaymentRead, status_code=status.HTTP_201_CREATED)
async def initiate_payment(
    order_id: int,
    request: PaymentInitiateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Initiate a payment attempt for the authenticated user's order."""
    service = PaymentService(db)
    payment = await service.initiate_payment(
        order_id=order_id,
        user=current_user,
        idempotency_key=request.idempotency_key,
        provider_name=request.provider,
    )
    return payment


@router.get("", response_model=list[PaymentRead], status_code=status.HTTP_200_OK)
async def list_order_payments(
    order_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Get payment attempts belonging to an order."""
    service = PaymentService(db)
    payments = await service.list_order_payments(
        order_id=order_id,
        user=current_user,
    )
    return payments
