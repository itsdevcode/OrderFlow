from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category


class CategoryRepository:
    db: AsyncSession

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, category_id: int) -> Category | None:
        result = await self.db.execute(
            select(Category).where(Category.id == category_id)
        )
        return result.scalar_one_or_none()

    async def get_by_name(self, name: str) -> Category | None:
        result = await self.db.execute(
            select(Category).where(Category.name == name)
        )
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> Category | None:
        result = await self.db.execute(
            select(Category).where(Category.slug == slug)
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> list[Category]:
        result = await self.db.execute(
            select(Category)
            .order_by(Category.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def create(
        self,
        *,
        name: str,
        slug: str,
        description: str | None = None,
        is_active: bool = True,
    ) -> Category:
        category = Category(
            name=name,
            slug=slug,
            description=description,
            is_active=is_active,
        )

        self.db.add(category)

        await self.db.flush()
        await self.db.refresh(category)

        return category

    async def update(
        self,
        category: Category,
        **changes: str | bool | None,
    ) -> Category:
        allowed_fields: set[str] = {
            "name",
            "slug",
            "description",
            "is_active",
        }
        for key, value in changes.items():
            if key in allowed_fields:
                setattr(category, key, value)

        await self.db.flush()
        await self.db.refresh(category)

        return category

    async def delete(
        self,
        category: Category,
    ) -> None:
        await self.db.delete(category)
        await self.db.flush()
