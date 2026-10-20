import logging
from typing import cast
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions.category import (
    CategoryAlreadyExistsError,
    CategoryHasProductsError,
    CategoryNotFoundError,
)
from app.models.category import Category
from app.repositories.category import CategoryRepository
from app.schemas.category import CategoryCreate, CategoryResponse, CategoryUpdate
from app.services.cache import CacheService, get_cache_service

logger = logging.getLogger(__name__)


class CategoryService:
    def __init__(self, db: AsyncSession, cache: CacheService | None = None) -> None:
        self.db: AsyncSession = db
        self.category_repo: CategoryRepository = CategoryRepository(db)
        self.cache: CacheService = cache or get_cache_service()

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

            # Persist durable cache-invalidation event in the same transaction
            events = self.cache.stage_category_invalidation(self.db, category.id)
            # Commit both atomically
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise CategoryAlreadyExistsError(
                "Category name or slug already exists"
            ) from exc

        # After commit, attempt Redis invalidation; acknowledge event only on success
        _ = await self.cache.execute_staged_invalidations(self.db, events)

        return category

    async def get_category_model(self, category_id: int) -> Category:
        """Fetch the authoritative Category ORM instance from PostgreSQL."""
        category = await self.category_repo.get_by_id(category_id)
        if not category:
            raise CategoryNotFoundError(f"Category with id {category_id} not found")
        return category

    async def get_category(self, category_id: int) -> CategoryResponse:
        """
        Cache-aside read for single category detail.
        Reconciles pending invalidations from Redis outages across multiple workers,
        attempts Redis lookup, falls back to PostgreSQL on miss or failure,
        and serializes validated response to Redis with bounded TTL.
        """
        sync_ok = await self.cache.sync_pending_invalidations(self.db)
        if not sync_ok:
            category = await self.get_category_model(category_id)
            return CategoryResponse.model_validate(category)

        cache_key = self.cache.build_key("categories", "detail", str(category_id))
        cached_data = await self.cache.get(cache_key)
        if cached_data is not None and isinstance(cached_data, dict):
            try:
                return CategoryResponse.model_validate(cached_data)
            except Exception as exc:
                logger.warning(
                    "Corrupted category cache for %s (%s); purging and falling back to DB",
                    cache_key,
                    exc,
                )
                _ = await self.cache.delete(cache_key)

        category = await self.get_category_model(category_id)
        response = CategoryResponse.model_validate(category)
        _ = await self.cache.set(cache_key, response.model_dump(mode="json"))
        return response

    async def list_categories(self, offset: int = 0, limit: int = 20) -> list[CategoryResponse]:
        """
        Cache-aside read for paginated category listings.
        Uses versioned namespace keys to enable O(1) bulk invalidation without Redis KEYS.
        Rejects entire cached list if any item is malformed or invalid to prevent partial data.
        """
        sync_ok = await self.cache.sync_pending_invalidations(self.db)
        if not sync_ok:
            categories = await self.category_repo.list(offset=offset, limit=limit)
            return [CategoryResponse.model_validate(c) for c in categories]

        version = await self.cache.get_version("categories")
        cache_key = self.cache.build_key(
            "categories",
            "list",
            f"v{version}",
            offset=offset,
            limit=limit,
        )

        cached_data = await self.cache.get(cache_key)
        if cached_data is not None and isinstance(cached_data, list):
            try:
                raw_list = cast(list[object], cached_data)
                validated_items: list[CategoryResponse] = []
                for item in raw_list:
                    if not isinstance(item, dict):
                        raise ValueError(f"Invalid non-dict item in cached category list: {type(item)}")
                    validated_items.append(CategoryResponse.model_validate(item))
                return validated_items
            except Exception as exc:
                logger.warning(
                    "Corrupted category list cache for %s (%s); purging and falling back to DB",
                    cache_key,
                    exc,
                )
                _ = await self.cache.delete(cache_key)

        categories = await self.category_repo.list(offset=offset, limit=limit)
        response_list = [CategoryResponse.model_validate(c) for c in categories]

        # Guard against read-version -> DB query -> cache SET race:
        # Only populate cache if the namespace version has not changed during the query
        v_after = await self.cache.get_version("categories")
        if v_after == version:
            _ = await self.cache.set(
                cache_key,
                [item.model_dump(mode="json") for item in response_list],
            )
        return response_list

    async def update_category(self, category_id: int, data: CategoryUpdate) -> Category:
        category = await self.get_category_model(category_id)

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
            events = self.cache.stage_category_invalidation(self.db, category_id)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise CategoryAlreadyExistsError("Category name or slug already exists") from exc

        # After commit, attempt Redis invalidation; acknowledge event only on success
        _ = await self.cache.execute_staged_invalidations(self.db, events)

        return updated_category

    async def delete_category(self, category_id: int) -> None:
        category = await self.get_category_model(category_id)
        category_name = category.name
        try:
            await self.category_repo.delete(category)
            events = self.cache.stage_category_invalidation(self.db, category_id)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise CategoryHasProductsError(
                f"Cannot delete category '{category_name}' as it has associated products"
            ) from exc

        # After commit, attempt Redis invalidation; acknowledge event only on success
        _ = await self.cache.execute_staged_invalidations(self.db, events)
