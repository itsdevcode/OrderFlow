from sqlalchemy.ext.asyncio import AsyncSession
from app.models.inventory_reservation import InventoryReservation, ReservationStatus
from app.repositories.inventory_reservation import InventoryReservationRepository
from app.repositories.inventory import InventoryRepository
from app.exceptions.inventory_reservation import ReservationNotFoundError, InvalidReservationStateError

class InventoryReservationService:
    db: AsyncSession
    reservation_repo: InventoryReservationRepository
    inventory_repo: InventoryRepository

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.reservation_repo = InventoryReservationRepository(db)
        self.inventory_repo = InventoryRepository(db)

    async def _transition_reservation(self, reservation_id: int, target_status: ReservationStatus) -> InventoryReservation:
        reservation = await self.reservation_repo.get_by_id(reservation_id, for_update=True)
        if not reservation:
            raise ReservationNotFoundError(f"Reservation {reservation_id} not found")

        if reservation.status == target_status:
            return reservation # Idempotent

        if reservation.status != ReservationStatus.ACTIVE:
            raise InvalidReservationStateError(f"Cannot transition reservation from {reservation.status} to {target_status}")

        inventory = await self.inventory_repo.get_by_product_and_warehouse(
            reservation.product_id, reservation.warehouse_id, for_update=True
        )
        if not inventory:
            raise ValueError("Inventory record missing for reservation")

        if target_status in (ReservationStatus.RELEASED, ReservationStatus.EXPIRED):
            inventory.available_quantity += reservation.quantity
            inventory.reserved_quantity -= reservation.quantity
        elif target_status == ReservationStatus.CONFIRMED:
            inventory.reserved_quantity -= reservation.quantity
            
        reservation.status = target_status
        return reservation

    async def release_reservation(self, reservation_id: int) -> InventoryReservation:
        try:
            reservation = await self._transition_reservation(reservation_id, ReservationStatus.RELEASED)
            await self.db.commit()
            return reservation
        except Exception:
            await self.db.rollback()
            raise

    async def confirm_reservation(self, reservation_id: int) -> InventoryReservation:
        try:
            reservation = await self._transition_reservation(reservation_id, ReservationStatus.CONFIRMED)
            await self.db.commit()
            return reservation
        except Exception:
            await self.db.rollback()
            raise

    async def expire_reservation(self, reservation_id: int) -> InventoryReservation:
        try:
            reservation = await self._transition_reservation(reservation_id, ReservationStatus.EXPIRED)
            await self.db.commit()
            return reservation
        except Exception:
            await self.db.rollback()
            raise
