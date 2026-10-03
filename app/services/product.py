from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions.category import CategoryNotFoundError
from app.exceptions.product import InvalidPriceError, ProductAlreadyExistsError, ProductNotFoundError
from app.models.product import Product
from app.repositories.category import CategoryRepository
from app.repositories.product import ProductRepository
from app.schemas.product import ProductCreate, ProductUpdate


class ProductService:
    def __init__(self, db: AsyncSession) -> None:
        self.db: AsyncSession = db
        self.product_repo: ProductRepository = ProductRepository(db)
        self.category_repo: CategoryRepository = CategoryRepository(db)

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

        return product

    async def get_product(self, product_id: int) -> Product:
        product = await self.product_repo.get_by_id(product_id)
        if not product:
            raise ProductNotFoundError(f"Product with id {product_id} not found")
        return product

    async def list_products(
        self,
        offset: int = 0,
        limit: int = 20,
        category_id: int | None = None,
        is_active: bool | None = None,
    ) -> list[Product]:
        return await self.product_repo.list(
            offset=offset,
            limit=limit,
            category_id=category_id,
            is_active=is_active,
        )

    async def update_product(self, product_id: int, data: ProductUpdate) -> Product:
        product = await self.get_product(product_id)

        changes = data.model_dump(exclude_unset=True)
        if not changes:
            return product

        if "category_id" in changes:
            category = await self.category_repo.get_by_id(changes["category_id"])
            if not category:
                raise CategoryNotFoundError(f"Category with id {changes['category_id']} not found")

        if "sku" in changes:
            existing = await self.product_repo.get_by_sku(changes["sku"])
            if existing and existing.id != product_id:
                raise ProductAlreadyExistsError(f"Product with sku '{changes['sku']}' already exists")

        if "slug" in changes:
            existing = await self.product_repo.get_by_slug(changes["slug"])
            if existing and existing.id != product_id:
                raise ProductAlreadyExistsError(f"Product with slug '{changes['slug']}' already exists")

        if "price" in changes and changes["price"] < 0:
            raise InvalidPriceError("Price cannot be negative")

        try:
            updated_product = await self.product_repo.update(product, **changes)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise ProductAlreadyExistsError("Product SKU or slug already exists") from exc

        return updated_product

    async def delete_product(self, product_id: int) -> None:
        product = await self.get_product(product_id)
        try:
            await self.product_repo.delete(product)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise ProductAlreadyExistsError("Cannot delete product due to database integrity constraints") from exc
