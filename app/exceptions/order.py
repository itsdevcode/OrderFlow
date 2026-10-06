from fastapi import HTTPException, status


class OrderNotFoundError(HTTPException):
    def __init__(self, detail: str = "Order not found"):
        super().__init__(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


class EmptyCartError(HTTPException):
    def __init__(self, detail: str = "Cannot create order from an empty cart"):
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


class InvalidOrderStateError(HTTPException):
    def __init__(self, detail: str = "Invalid order state transition"):
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
