from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.models.category import Category
from app.repositories.category import CategoryRepository
from app.schemas.category import CategoryCreate, CategoryUpdate
from app.exceptions.category import (
    CategoryAlreadyExistsError,
    CategoryNotFoundError,
    CategoryHasProductsError,
)


class CategoryService:
    def __init__(self, db: AsyncSession) -> None:
        self.db: AsyncSession = db
        self.category_repo: CategoryRepository = CategoryRepository(db)

    async def create_category(self, data: CategoryCreate) -> Category:
        # Check if name already exists
        if await self.category_repo.get_by_name(data.name):
            raise CategoryAlreadyExistsError(f"Category with name '{data.name}' already exists")

        # Check if slug already exists
        if await self.category_repo.get_by_slug(data.slug):
            raise CategoryAlreadyExistsError(f"Category with slug '{data.slug}' already exists")

        try:
            # Create category using repository
            category = await self.category_repo.create(
                name=data.name,
                slug=data.slug,
                description=data.description,
            )
    
            # Transaction owned by service
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise CategoryAlreadyExistsError(
                "Category name or slug already exists"
            ) from exc
    
        return category

    async def list_categories(self, offset: int = 0, limit: int = 20) -> list[Category]:
        return await self.category_repo.list(offset=offset, limit=limit)

    async def get_category(self, category_id: int) -> Category:
        category = await self.category_repo.get_by_id(category_id)
        if not category:
            raise CategoryNotFoundError(f"Category with id {category_id} not found")
        return category

    async def update_category(self, category_id: int, data: CategoryUpdate) -> Category:
        category = await self.get_category(category_id)

        changes: dict[str, str | bool | None] = data.model_dump(exclude_unset=True)
        if not changes:
            return category

        # Check if name is being updated and conflicts
        if "name" in changes and isinstance(changes["name"], str):
            existing = await self.category_repo.get_by_name(str(changes["name"]))
            if existing and existing.id != category_id:
                raise CategoryAlreadyExistsError(f"Category with name '{changes['name']}' already exists")

        # Check if slug is being updated and conflicts
        if "slug" in changes and isinstance(changes["slug"], str):
            existing = await self.category_repo.get_by_slug(str(changes["slug"]))
            if existing and existing.id != category_id:
                raise CategoryAlreadyExistsError(f"Category with slug '{changes['slug']}' already exists")

        try:
            updated_category = await self.category_repo.update(category, **changes)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise CategoryAlreadyExistsError("Category name or slug already exists") from exc

        return updated_category

    async def delete_category(self, category_id: int) -> None:
        category = await self.get_category(category_id)
        category_name = category.name
        try:
            await self.category_repo.delete(category)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise CategoryHasProductsError(
                f"Cannot delete category '{category_name}' as it has associated products"
            ) from exc
