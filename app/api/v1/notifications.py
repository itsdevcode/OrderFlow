from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies.auth import CurrentUser
from app.exceptions.notification import NotificationNotFoundError
from app.models.notification import NotificationStatus, NotificationType
from app.schemas.notification import NotificationRead
from app.services.notification import NotificationService

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("", response_model=list[NotificationRead], status_code=status.HTTP_200_OK)
async def list_notifications(
    current_user: CurrentUser,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    status_filter: Annotated[NotificationStatus | None, Query(alias="status")] = None,
    type_filter: Annotated[NotificationType | None, Query(alias="type")] = None,
    db: AsyncSession = Depends(get_db),
) -> list[NotificationRead]:
    """
    Returns notifications for the authenticated user with bounded pagination and optional filtering.
    """
    service = NotificationService(db)
    items = await service.list_by_user(
        user_id=current_user.id,
        offset=offset,
        limit=limit,
        status=status_filter,
        notification_type=type_filter,
    )
    return [NotificationRead.model_validate(item) for item in items]


@router.get("/{notification_id}", response_model=NotificationRead, status_code=status.HTTP_200_OK)
async def get_notification(
    notification_id: int,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> NotificationRead:
    """
    Get a specific notification by ID.
    Ownership-protected: only the recipient can view it; non-owned notifications return 404.
    """
    service = NotificationService(db)
    notification = await service.get_by_id(
        notification_id=notification_id,
        user_id=current_user.id,
    )
    return NotificationRead.model_validate(notification)
