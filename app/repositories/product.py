from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.product import Product


class ProductRepository:
    db: AsyncSession

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, product_id: int) -> Product | None:
        stmt = (
            select(Product)
            .options(selectinload(Product.category))
            .where(Product.id == product_id)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_sku(self, sku: str) -> Product | None:
        stmt = (
            select(Product)
            .options(selectinload(Product.category))
            .where(Product.sku == sku)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> Product | None:
        stmt = (
            select(Product)
            .options(selectinload(Product.category))
            .where(Product.slug == slug)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        offset: int = 0,
        limit: int = 20,
        category_id: int | None = None,
        is_active: bool | None = None,
    ) -> list[Product]:
        stmt = select(Product).options(selectinload(Product.category))

        if category_id is not None:
            stmt = stmt.where(Product.category_id == category_id)

        if is_active is not None:
            stmt = stmt.where(Product.is_active == is_active)

        stmt = (
            stmt
            .order_by(Product.created_at.desc())
            .offset(offset)
            .limit(limit)
        )

        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def create(
        self,
        *,
        name: str,
        slug: str,
        sku: str,
        description: str | None = None,
        price: Decimal,
        category_id: int,
        is_active: bool = True,
    ) -> Product:
        product = Product(
            name=name,
            slug=slug,
            sku=sku,
            description=description,
            price=price,
            category_id=category_id,
            is_active=is_active,
        )

        self.db.add(product)

        await self.db.flush()
        await self.db.refresh(product)

        return product

    async def update(
        self,
        product: Product,
        **changes: object,
    ) -> Product:
        allowed_fields: set[str] = {
            "name",
            "slug",
            "sku",
            "description",
            "price",
            "category_id",
            "is_active",
        }
        for key, value in changes.items():
            if key in allowed_fields:
                setattr(product, key, value)

        await self.db.flush()
        await self.db.refresh(product)

        result = await self.get_by_id(product.id)
        assert result is not None
        return result

    async def delete(
        self,
        product: Product,
    ) -> None:
        await self.db.delete(product)
        await self.db.flush()
