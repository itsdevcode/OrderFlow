import logging
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions.category import CategoryNotFoundError
from app.exceptions.product import InvalidPriceError, ProductAlreadyExistsError, ProductNotFoundError
from app.models.product import Product
from app.repositories.category import CategoryRepository
from app.repositories.product import ProductRepository
from app.schemas.product import ProductCreate, ProductRead, ProductUpdate
from app.services.cache import CacheService, get_cache_service

logger = logging.getLogger(__name__)


class ProductService:
    def __init__(self, db: AsyncSession, cache: CacheService | None = None) -> None:
        self.db: AsyncSession = db
        self.product_repo: ProductRepository = ProductRepository(db)
        self.category_repo: CategoryRepository = CategoryRepository(db)
        self.cache: CacheService = cache or get_cache_service()

    async def create_product(self, data: ProductCreate) -> Product:
        # Check if category exists
        category = await self.category_repo.get_by_id(data.category_id)
        if not category:
            raise CategoryNotFoundError(f"Category with id {data.category_id} not found")

        # Check if SKU unique
        if await self.product_repo.get_by_sku(data.sku):
            raise ProductAlreadyExistsError(f"Product with sku '{data.sku}' already exists")

        # Check if slug unique
        if await self.product_repo.get_by_slug(data.slug):
            raise ProductAlreadyExistsError(f"Product with slug '{data.slug}' already exists")

        # Check if price valid
        if data.price < 0:
            raise InvalidPriceError("Price must be greater than or equal to zero")

        try:
            # Create product using repository
            product = await self.product_repo.create(
                name=data.name,
                slug=data.slug,
                sku=data.sku,
                description=data.description,
                price=data.price,
                category_id=data.category_id,
                is_active=data.is_active,
            )

            # Transaction owned by service
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise ProductAlreadyExistsError(
                "Product SKU or slug already exists"
            ) from exc

        # Invalidate listing caches after successful commit
        _ = await self.cache.bump_version("products")

        return product

    async def get_product_model(self, product_id: int) -> Product:
        """Fetch the authoritative Product ORM instance from PostgreSQL."""
        product = await self.product_repo.get_by_id(product_id)
        if not product:
            raise ProductNotFoundError(f"Product with id {product_id} not found")
        return product

    async def get_product(self, product_id: int) -> ProductRead:
        """
        Cache-aside read for single product detail.
        Attempts Redis lookup, falls back to PostgreSQL on miss or failure,
        and serializes validated response to Redis with bounded TTL.
        """
        cache_key = self.cache.build_key("products", "detail", str(product_id))
        cached_data = await self.cache.get(cache_key)
        if cached_data is not None and isinstance(cached_data, dict):
            try:
                return ProductRead.model_validate(cached_data)
            except Exception as exc:
                logger.warning("Corrupted product cache for %s (%s); falling back to DB", cache_key, exc)

        product = await self.get_product_model(product_id)
        response = ProductRead.model_validate(product)
        _ = await self.cache.set(cache_key, response.model_dump(mode="json"))
        return response

    async def list_products(
        self,
        offset: int = 0,
        limit: int = 20,
        category_id: int | None = None,
        is_active: bool | None = None,
    ) -> list[ProductRead]:
        """
        Cache-aside read for paginated and filtered product listings.
        Uses versioned namespace keys to enable O(1) bulk invalidation without Redis KEYS.
        """
        version = await self.cache.get_version("products")
        cache_key = self.cache.build_key(
            "products",
            "list",
            f"v{version}",
            offset=offset,
            limit=limit,
            category_id=category_id,
            is_active=is_active,
        )

        cached_data = await self.cache.get(cache_key)
        if cached_data is not None and isinstance(cached_data, list):
            try:
                from typing import cast
                raw_list = cast(list[object], cached_data)
                validated_items: list[ProductRead] = []
                for item in raw_list:
                    if isinstance(item, dict):
                        validated_items.append(ProductRead.model_validate(item))
                return validated_items
            except Exception as exc:
                logger.warning("Corrupted product list cache for %s (%s); falling back to DB", cache_key, exc)

        products = await self.product_repo.list(
            offset=offset,
            limit=limit,
            category_id=category_id,
            is_active=is_active,
        )
        response_list = [ProductRead.model_validate(p) for p in products]
        _ = await self.cache.set(
            cache_key,
            [item.model_dump(mode="json") for item in response_list],
        )
        return response_list

    async def update_product(self, product_id: int, data: ProductUpdate) -> Product:
        product = await self.get_product_model(product_id)

        changes: dict[str, object] = data.model_dump(exclude_unset=True)
        if not changes:
            return product

        if "category_id" in changes and changes["category_id"] is not None:
            cat_id_raw = changes["category_id"]
            if isinstance(cat_id_raw, int):
                category = await self.category_repo.get_by_id(cat_id_raw)
                if not category:
                    raise CategoryNotFoundError(f"Category with id {cat_id_raw} not found")

        if "sku" in changes and isinstance(changes["sku"], str):
            existing = await self.product_repo.get_by_sku(changes["sku"])
            if existing and existing.id != product_id:
                raise ProductAlreadyExistsError(f"Product with sku '{changes['sku']}' already exists")

        if "slug" in changes and isinstance(changes["slug"], str):
            existing = await self.product_repo.get_by_slug(changes["slug"])
            if existing and existing.id != product_id:
                raise ProductAlreadyExistsError(f"Product with slug '{changes['slug']}' already exists")

        if "price" in changes and changes["price"] is not None:
            price_val = changes["price"]
            if isinstance(price_val, (int, float, str)) and float(str(price_val)) < 0:
                raise InvalidPriceError("Price cannot be negative")

        try:
            updated_product = await self.product_repo.update(product, **changes)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise ProductAlreadyExistsError("Product SKU or slug already exists") from exc

        # Invalidate affected product caches after successful commit
        await self.cache.invalidate_product(product_id)

        return updated_product

    async def delete_product(self, product_id: int) -> None:
        product = await self.get_product_model(product_id)
        try:
            await self.product_repo.delete(product)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise ProductAlreadyExistsError("Cannot delete product due to database integrity constraints") from exc

        # Invalidate affected product caches after successful commit
        await self.cache.invalidate_product(product_id)
