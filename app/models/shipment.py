from datetime import datetime
from enum import Enum as PyEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.order import Order


class ShipmentStatus(str, PyEnum):
    PENDING = "PENDING"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


class Shipment(Base):
    __tablename__: str = "shipments"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
        index=True,
    )
    tracking_number: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    carrier: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    status: Mapped[ShipmentStatus] = mapped_column(
        Enum(ShipmentStatus, name="shipmentstatus_enum", native_enum=False),
        nullable=False,
        default=ShipmentStatus.PENDING,
        index=True,
    )
    shipped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
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

    __table_args__: tuple[Index, ...] = (
        Index(
            "uq_shipments_carrier_tracking_number",
            "carrier",
            "tracking_number",
            unique=True,
            postgresql_where=(carrier.isnot(None) & tracking_number.isnot(None)),
        ),
    )

    # Relationship
    order: Mapped["Order"] = relationship("Order", back_populates="shipment")
