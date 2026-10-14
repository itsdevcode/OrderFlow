from collections.abc import Sequence
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.background import dispatch_notification_task
from app.core.roles import RoleName
from app.exceptions.order import OrderNotFoundError
from app.exceptions.shipment import (
    DuplicateShipmentError,
    InvalidShipmentStateTransitionError,
    OrderNotEligibleForShipmentError,
    ShipmentNotFoundError,
    TrackingConflictError,
)
from app.models.notification import NotificationType
from app.models.order import Order, OrderStatus
from app.models.shipment import Shipment, ShipmentStatus
from app.models.user import User
from app.repositories.payment import PaymentRepository
from app.repositories.shipment import ShipmentRepository
from app.schemas.shipment import ShipmentStatusUpdate
from app.services.notification import NotificationService
from fastapi import BackgroundTasks


class ShipmentService:
    db: AsyncSession
    shipment_repo: ShipmentRepository
    payment_repo: PaymentRepository

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.shipment_repo = ShipmentRepository(db)
        self.payment_repo = PaymentRepository(db)

    def _get_utc_now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def _get_authorized_order(
        self,
        order_id: int,
        user: User,
        for_update: bool = False,
    ) -> Order:
        stmt = (
            select(Order)
            .options(selectinload(Order.items), selectinload(Order.shipment))
            .where(Order.id == order_id)
        )
        if for_update:
            stmt = stmt.with_for_update()

        result = await self.db.execute(stmt)
        order = result.scalar_one_or_none()

        if not order:
            raise OrderNotFoundError("Order not found")

        # Isolation check: only owner or ADMIN/MANAGER may view or act
        is_owner = order.user_id == user.id
        is_staff = user.role.name in (RoleName.ADMIN.value, RoleName.MANAGER.value)
        if not (is_owner or is_staff):
            raise OrderNotFoundError("Order not found")

        return order

    async def create_shipment(
        self,
        order_id: int,
        user: User,
        carrier: str | None = None,
        tracking_number: str | None = None,
    ) -> Shipment:
        """
        Create a shipment for an order.
        Requirements:
        - ADMIN / MANAGER only
        - Order exists
        - Order status CONFIRMED
        - Successfully completed payment (SUCCEEDED) associated with that order
        - No existing shipment for that order
        - Lock order row with SELECT ... FOR UPDATE before shipment creation
        - Handle concurrent duplicate requests deterministically (no unhandled 500)
        """
        # Lock ordering: 1. Order (FOR UPDATE)
        try:
            # Step 1: Acquire row lock on Order
            order = await self._get_authorized_order(order_id, user, for_update=True)

            # Step 2: Validate order status is CONFIRMED
            if order.status != OrderStatus.CONFIRMED:
                raise OrderNotEligibleForShipmentError(
                    f"Order {order_id} is in status '{order.status.value}'. Only CONFIRMED orders are eligible for shipment."
                )

            # Step 3: Check for successfully completed payment
            succeeded_payment = await self.payment_repo.get_succeeded_by_order_id(order_id)
            if not succeeded_payment:
                raise OrderNotEligibleForShipmentError(
                    f"Order {order_id} does not have a successfully completed payment."
                )

            # Step 4: Check if shipment already exists
            existing_shipment = await self.shipment_repo.get_by_order_id_for_update(order_id)
            if existing_shipment:
                raise DuplicateShipmentError(
                    f"A shipment already exists for order {order_id}."
                )

            # Step 5: Check tracking number uniqueness if provided on creation
            if carrier and tracking_number:
                existing_tracking = await self.shipment_repo.get_by_carrier_and_tracking(
                    carrier, tracking_number
                )
                if existing_tracking:
                    raise TrackingConflictError(
                        f"Tracking number '{tracking_number}' for carrier '{carrier}' is already assigned to another shipment."
                    )

            # Create shipment with PENDING status
            shipment = Shipment(
                order_id=order_id,
                carrier=carrier,
                tracking_number=tracking_number,
                status=ShipmentStatus.PENDING,
            )

            await self.shipment_repo.create(shipment)
            await self.db.commit()
            await self.db.refresh(shipment)
            return shipment

        except (DuplicateShipmentError, OrderNotEligibleForShipmentError, TrackingConflictError, HTTPException):
            await self.db.rollback()
            raise
        except IntegrityError as exc:
            await self.db.rollback()
            # Handle concurrent race condition on order_id unique constraint or tracking index
            existing = await self.shipment_repo.get_by_order_id(order_id)
            if existing:
                raise DuplicateShipmentError(
                    f"A shipment already exists for order {order_id}."
                )
            if carrier and tracking_number:
                existing_tracking = await self.shipment_repo.get_by_carrier_and_tracking(carrier, tracking_number)
                if existing_tracking:
                    raise TrackingConflictError(
                        f"Tracking number '{tracking_number}' for carrier '{carrier}' is already assigned to another shipment."
                    )
            raise DuplicateShipmentError(
                f"Shipment creation conflict for order {order_id}."
            ) from exc
        except Exception:
            await self.db.rollback()
            raise

    async def get_shipment_by_order_id(
        self,
        order_id: int,
        user: User,
    ) -> Shipment:
        """
        Get shipment for an order.
        ADMIN / MANAGER: allowed.
        CUSTOMER: allowed only for owned orders (non-owned returns 404).
        """
        _ = await self._get_authorized_order(order_id, user, for_update=False)

        shipment = await self.shipment_repo.get_by_order_id(order_id)
        if not shipment:
            raise ShipmentNotFoundError(f"No shipment found for order {order_id}.")
        return shipment

    async def get_shipment_by_id(
        self,
        shipment_id: int,
        user: User,
    ) -> Shipment:
        """
        Get shipment by ID.
        ADMIN / MANAGER: allowed.
        CUSTOMER: allowed only when shipment belongs to their order (non-owned returns 404).
        """
        shipment = await self.shipment_repo.get_by_id(shipment_id)
        if not shipment:
            raise ShipmentNotFoundError(f"Shipment with id {shipment_id} not found.")

        # Check order ownership / authorization
        is_staff = user.role.name in (RoleName.ADMIN.value, RoleName.MANAGER.value)
        is_owner = shipment.order.user_id == user.id

        if not (is_staff or is_owner):
            raise ShipmentNotFoundError(f"Shipment with id {shipment_id} not found.")

        return shipment

    async def list_shipments(
        self,
        offset: int = 0,
        limit: int = 20,
        status: ShipmentStatus | None = None,
    ) -> Sequence[Shipment]:
        """
        List shipments with pagination and optional status filtering.
        ADMIN / MANAGER only.
        """
        return await self.shipment_repo.list_shipments(offset=offset, limit=limit, status=status)

    async def transition_shipment_status(
        self,
        shipment_id: int,
        update_data: ShipmentStatusUpdate,
        background_tasks: BackgroundTasks | None = None,
    ) -> Shipment:
        """
        Transition shipment status with strict state machine and locking.
        Lock ordering:
        1. Order row lock (if order is loaded) or Shipment row lock FOR UPDATE
        To maintain consistent lock ordering across the application:
        - We first lock the Order associated with the shipment (or lock shipment, then order if needed).
        Specifically, we fetch the shipment row with FOR UPDATE. If order locking is needed, lock order first.
        Let's lock Order row first if known, or lock Shipment row FOR UPDATE.
        State transitions:
        - PENDING -> SHIPPED: requires carrier and tracking_number
        - PENDING -> CANCELLED: sets cancelled_at
        - SHIPPED -> DELIVERED: sets delivered_at, preserves carrier and tracking_number
        Terminal states:
        - DELIVERED, CANCELLED
        Idempotency:
        - Repeated request targeting current state is idempotent ONLY when compatible with stored shipment data.
        - Incompatible repeated request raises 409 Conflict.
        """
        target_status = update_data.status
        carrier = update_data.carrier
        tracking_number = update_data.tracking_number

        try:
            # Lock shipment row FOR UPDATE
            shipment = await self.shipment_repo.get_by_id_for_update(shipment_id)
            if not shipment:
                raise ShipmentNotFoundError(f"Shipment with id {shipment_id} not found.")

            current_status = shipment.status

            # Check Idempotency for repeated request targeting the current state
            if current_status == target_status:
                if target_status == ShipmentStatus.SHIPPED:
                    # Must be compatible with existing carrier and tracking_number
                    if carrier and carrier != shipment.carrier:
                        raise InvalidShipmentStateTransitionError(
                            f"Shipment is already SHIPPED with carrier '{shipment.carrier}'. Cannot change carrier to '{carrier}'."
                        )
                    if tracking_number and tracking_number != shipment.tracking_number:
                        raise InvalidShipmentStateTransitionError(
                            f"Shipment is already SHIPPED with tracking number '{shipment.tracking_number}'. Cannot change tracking number to '{tracking_number}'."
                        )
                elif target_status in (ShipmentStatus.DELIVERED, ShipmentStatus.CANCELLED):
                    # Repeating terminal state is idempotent
                    pass
                return shipment

            # Validate state transitions
            allowed_transitions: dict[ShipmentStatus, set[ShipmentStatus]] = {
                ShipmentStatus.PENDING: {ShipmentStatus.SHIPPED, ShipmentStatus.CANCELLED},
                ShipmentStatus.SHIPPED: {ShipmentStatus.DELIVERED},
                ShipmentStatus.DELIVERED: set(),
                ShipmentStatus.CANCELLED: set(),
            }

            if target_status not in allowed_transitions.get(current_status, set()):
                raise InvalidShipmentStateTransitionError(
                    f"Cannot transition shipment from {current_status.value} to {target_status.value}."
                )

            now = self._get_utc_now()
            pending_notification_id: int | None = None

            # Transition-specific validations and mutations
            if target_status == ShipmentStatus.SHIPPED:
                # Requires carrier and tracking_number
                effective_carrier = carrier or shipment.carrier
                effective_tracking = tracking_number or shipment.tracking_number

                if not effective_carrier or not effective_tracking:
                    raise InvalidShipmentStateTransitionError(
                        "Transition to SHIPPED requires both 'carrier' and 'tracking_number'."
                    )

                # Check unique carrier/tracking_number pair in other shipments
                existing_tracking = await self.shipment_repo.get_by_carrier_and_tracking(
                    effective_carrier, effective_tracking
                )
                if existing_tracking and existing_tracking.id != shipment.id:
                    raise TrackingConflictError(
                        f"Tracking number '{effective_tracking}' for carrier '{effective_carrier}' is already assigned to another shipment."
                    )

                shipment.carrier = effective_carrier
                shipment.tracking_number = effective_tracking
                shipment.status = ShipmentStatus.SHIPPED
                shipment.shipped_at = now

                # Create SHIPMENT_SHIPPED notification in the same transaction
                notification_service = NotificationService(self.db)
                notif = await notification_service.create_pending_notification(
                    user_id=shipment.order.user_id,
                    notification_type=NotificationType.SHIPMENT_SHIPPED,
                    order_id=shipment.order.id,
                    order_number=shipment.order.order_number,
                    deduplication_key=f"shipment.shipped:{shipment.id}",
                )
                if notif:
                    pending_notification_id = notif.id

            elif target_status == ShipmentStatus.DELIVERED:
                # Preserve original shipped_at, tracking_number, and carrier
                shipment.status = ShipmentStatus.DELIVERED
                shipment.delivered_at = now

                # Create SHIPMENT_DELIVERED notification in the same transaction
                notification_service = NotificationService(self.db)
                notif = await notification_service.create_pending_notification(
                    user_id=shipment.order.user_id,
                    notification_type=NotificationType.SHIPMENT_DELIVERED,
                    order_id=shipment.order.id,
                    order_number=shipment.order.order_number,
                    deduplication_key=f"shipment.delivered:{shipment.id}",
                )
                if notif:
                    pending_notification_id = notif.id

            elif target_status == ShipmentStatus.CANCELLED:
                shipment.status = ShipmentStatus.CANCELLED
                shipment.cancelled_at = now

            await self.db.commit()
            _ = await self.db.refresh(shipment)

            # Step: Dispatch background task after successful commit
            if background_tasks and pending_notification_id is not None:
                background_tasks.add_task(dispatch_notification_task, pending_notification_id)

            return shipment

        except (InvalidShipmentStateTransitionError, ShipmentNotFoundError, TrackingConflictError, HTTPException):
            await self.db.rollback()
            raise
        except IntegrityError as exc:
            await self.db.rollback()
            # If carrier+tracking unique constraint tripped concurrently
            if target_status == ShipmentStatus.SHIPPED and carrier and tracking_number:
                existing = await self.shipment_repo.get_by_carrier_and_tracking(carrier, tracking_number)
                if existing and existing.id != shipment_id:
                    raise TrackingConflictError(
                        f"Tracking number '{tracking_number}' for carrier '{carrier}' is already assigned to another shipment."
                    )
            raise TrackingConflictError("Shipment update conflict due to concurrent modification.") from exc
        except Exception:
            await self.db.rollback()
            raise
