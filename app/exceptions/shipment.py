from fastapi import HTTPException, status


class ShipmentNotFoundError(HTTPException):
    def __init__(self, detail: str = "Shipment not found") -> None:
        super().__init__(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


class OrderNotEligibleForShipmentError(HTTPException):
    def __init__(self, detail: str = "Order is not eligible for shipment creation") -> None:
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=detail)


class DuplicateShipmentError(HTTPException):
    def __init__(self, detail: str = "A shipment already exists for this order") -> None:
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=detail)


class InvalidShipmentStateTransitionError(HTTPException):
    def __init__(self, detail: str = "Invalid shipment state transition") -> None:
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=detail)


class TrackingConflictError(HTTPException):
    def __init__(self, detail: str = "Shipment tracking number conflict") -> None:
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=detail)
