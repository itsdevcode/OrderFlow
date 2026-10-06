from datetime import datetime
from typing import TYPE_CHECKING
from enum import Enum as PyEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Enum, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.order import Order
    from app.models.product import Product
    from app.models.warehouse import Warehouse

class ReservationStatus(str, PyEnum):
    ACTIVE = "ACTIVE"
    CONFIRMED = "CONFIRMED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"

class InventoryReservation(Base):
    __tablename__: str = "inventory_reservations"
    __table_args__: tuple[CheckConstraint, ...] = (
        CheckConstraint("quantity > 0", name="chk_reservation_quantity_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus, name="reservationstatus_enum", native_enum=False),
        nullable=False,
        default=ReservationStatus.ACTIVE,
        index=True
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        onupdate=func.now(),
        nullable=True,
    )

    order: Mapped["Order"] = relationship()
    product: Mapped["Product"] = relationship()
    warehouse: Mapped["Warehouse"] = relationship()
