class InventoryReservationError(Exception):
    pass

class ReservationNotFoundError(InventoryReservationError):
    pass

class InvalidReservationStateError(InventoryReservationError):
    pass
