from datetime import datetime
from decimal import Decimal
from enum import Enum as PyEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.order import Order


class PaymentStatus(str, PyEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class Payment(Base):
    __tablename__: str = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String, nullable=False)
    provider_payment_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(
        String,
        unique=True,
        nullable=False,
        index=True,
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
    )
    currency: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="INR",
        server_default="INR",
    )
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="paymentstatus_enum", native_enum=False),
        nullable=False,
        default=PaymentStatus.PENDING,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=True,
    )

    __table_args__: tuple = (
        CheckConstraint("amount >= 0", name="chk_payment_amount_non_negative"),
        Index(
            "uq_payments_order_id_pending",
            "order_id",
            unique=True,
            postgresql_where=(status == PaymentStatus.PENDING),
        ),
        Index(
            "uq_payments_order_id_succeeded",
            "order_id",
            unique=True,
            postgresql_where=(status == PaymentStatus.SUCCEEDED),
        ),
    )

    # Relationships
    order: Mapped["Order"] = relationship("Order", back_populates="payments")
