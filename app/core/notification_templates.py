from app.models.notification import NotificationType


def get_notification_template(
    notification_type: NotificationType, order_number: str
) -> tuple[str, str]:
    """
    Returns (title, message) deterministically for a notification type and order number.
    Safe templates that never disclose sensitive payment information.
    """
    if notification_type == NotificationType.ORDER_CREATED:
        return (
            "Order Created",
            f"Your order {order_number} has been created.",
        )
    elif notification_type == NotificationType.PAYMENT_SUCCEEDED:
        return (
            "Payment Succeeded",
            f"Payment for order {order_number} was successful.",
        )
    elif notification_type == NotificationType.PAYMENT_FAILED:
        return (
            "Payment Failed",
            f"Payment for order {order_number} failed. You can retry.",
        )
    elif notification_type == NotificationType.SHIPMENT_SHIPPED:
        return (
            "Order Shipped",
            f"Your order {order_number} has shipped.",
        )
    elif notification_type == NotificationType.SHIPMENT_DELIVERED:
        return (
            "Order Delivered",
            f"Your order {order_number} has been delivered.",
        )
    else:
        return (
            "Order Update",
            f"Update for order {order_number}.",
        )
