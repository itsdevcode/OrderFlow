from fastapi import HTTPException, status


class PaymentNotFoundError(HTTPException):
    def __init__(self, detail: str = "Payment not found"):
        super().__init__(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


class IdempotencyKeyConflictError(HTTPException):
    def __init__(self, detail: str = "Idempotency key has already been used for another request"):
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=detail)


class PaymentOrderCancelledError(HTTPException):
    def __init__(self, detail: str = "Cannot initiate payment for a cancelled order"):
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


class PaymentOrderAlreadyPaidError(HTTPException):
    def __init__(self, detail: str = "Order has already been paid"):
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=detail)


class PendingPaymentExistsError(HTTPException):
    def __init__(self, detail: str = "A pending payment is already active for this order"):
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=detail)


class InvalidPaymentStateTransitionError(HTTPException):
    def __init__(self, detail: str = "Invalid payment state transition"):
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


class PaymentProviderError(HTTPException):
    def __init__(self, detail: str = "Payment provider processing failed"):
        super().__init__(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)


class UnsupportedPaymentProviderError(HTTPException):
    def __init__(self, detail: str = "Unsupported payment provider"):
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)

