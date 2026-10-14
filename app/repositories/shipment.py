from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.shipment import Shipment, ShipmentStatus


class ShipmentRepository:
    db: AsyncSession

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, shipment_id: int) -> Shipment | None:
        stmt = (
            select(Shipment)
            .options(selectinload(Shipment.order))
            .where(Shipment.id == shipment_id)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id_for_update(self, shipment_id: int) -> Shipment | None:
        stmt = (
            select(Shipment)
            .options(selectinload(Shipment.order))
            .where(Shipment.id == shipment_id)
            .with_for_update()
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_order_id(self, order_id: int) -> Shipment | None:
        stmt = (
            select(Shipment)
            .options(selectinload(Shipment.order))
            .where(Shipment.order_id == order_id)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_order_id_for_update(self, order_id: int) -> Shipment | None:
        stmt = (
            select(Shipment)
            .options(selectinload(Shipment.order))
            .where(Shipment.order_id == order_id)
            .with_for_update()
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_carrier_and_tracking(
        self, carrier: str, tracking_number: str
    ) -> Shipment | None:
        stmt = select(Shipment).where(
            Shipment.carrier == carrier,
            Shipment.tracking_number == tracking_number,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, shipment: Shipment) -> Shipment:
        self.db.add(shipment)
        await self.db.flush()
        return shipment

    async def list_shipments(
        self,
        offset: int = 0,
        limit: int = 20,
        status: ShipmentStatus | None = None,
    ) -> Sequence[Shipment]:
        stmt = (
            select(Shipment)
            .options(selectinload(Shipment.order))
            .order_by(Shipment.created_at.desc(), Shipment.id.desc())
            .offset(offset)
            .limit(limit)
        )
        if status is not None:
            stmt = stmt.where(Shipment.status == status)

        result = await self.db.execute(stmt)
        return result.scalars().all()
