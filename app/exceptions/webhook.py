from fastapi import HTTPException, status


class WebhookAuthenticationError(HTTPException):
    def __init__(self, detail: str = "Invalid or missing webhook signature"):
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


class WebhookPayloadError(HTTPException):
    def __init__(self, detail: str = "Malformed or invalid webhook payload"):
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


class WebhookPayloadTooLargeError(HTTPException):
    def __init__(self, detail: str = "Webhook payload exceeds maximum size limit"):
        super().__init__(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=detail)


class WebhookEventConflictError(HTTPException):
    def __init__(self, detail: str = "Webhook event ID reused with contradictory payload"):
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=detail)


class WebhookReconciliationRequiredError(HTTPException):
    def __init__(self, detail: str = "Payment succeeded but reservations are expired or released. Quarantined for reconciliation."):
        super().__init__(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)
