from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions.cart import CartItemNotFoundError, InvalidQuantityError, ProductNotActiveError
from app.exceptions.product import ProductNotFoundError
from app.models.cart import Cart
from app.repositories.cart import CartRepository
from app.repositories.product import ProductRepository
from app.schemas.cart import CartItemAdd, CartItemUpdate


class CartService:
    db: AsyncSession
    cart_repo: CartRepository
    product_repo: ProductRepository

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.cart_repo = CartRepository(db)
        self.product_repo = ProductRepository(db)

    async def get_or_create_cart(self, user_id: int) -> Cart:
        cart = await self.cart_repo.get_by_user_id(user_id)
        if not cart:
            try:
                cart = await self.cart_repo.create_cart(user_id)
                await self.db.commit()
                # Refetch to get empty items list properly initialized
                cart = await self.cart_repo.get_by_user_id(user_id)
                assert cart is not None
            except IntegrityError:
                await self.db.rollback()
                # In case of concurrent creation
                cart = await self.cart_repo.get_by_user_id(user_id)
                assert cart is not None
        return cart

    async def add_item(self, user_id: int, data: CartItemAdd) -> Cart:
        if data.quantity <= 0:
            raise InvalidQuantityError("Quantity must be positive")

        product = await self.product_repo.get_by_id(data.product_id)
        if not product:
            raise ProductNotFoundError(f"Product with id {data.product_id} not found")

        if not product.is_active:
            raise ProductNotActiveError(f"Product '{product.name}' is not active")

        cart = await self.get_or_create_cart(user_id)

        try:
            _ = await self.cart_repo.upsert_item(cart.id, data.product_id, data.quantity)
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise

        # Refetch cart to return updated state
        updated_cart = await self.cart_repo.get_by_user_id(user_id)
        assert updated_cart is not None
        return updated_cart

    async def update_item(self, user_id: int, item_id: int, data: CartItemUpdate) -> Cart:
        if data.quantity <= 0:
            raise InvalidQuantityError("Quantity must be positive")

        cart = await self.get_or_create_cart(user_id)
        
        item = await self.cart_repo.get_item_by_id(item_id)
        if not item or item.cart_id != cart.id:
            raise CartItemNotFoundError(f"Cart item with id {item_id} not found in your cart")

        try:
            _ = await self.cart_repo.update_item(item, data.quantity)
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise

        updated_cart = await self.cart_repo.get_by_user_id(user_id)
        assert updated_cart is not None
        return updated_cart

    async def remove_item(self, user_id: int, item_id: int) -> Cart:
        cart = await self.get_or_create_cart(user_id)
        
        item = await self.cart_repo.get_item_by_id(item_id)
        if not item or item.cart_id != cart.id:
            raise CartItemNotFoundError(f"Cart item with id {item_id} not found in your cart")

        try:
            await self.cart_repo.delete_item(item)
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise

        updated_cart = await self.cart_repo.get_by_user_id(user_id)
        assert updated_cart is not None
        return updated_cart

    async def clear_cart(self, user_id: int) -> Cart:
        cart = await self.get_or_create_cart(user_id)
        try:
            await self.cart_repo.clear_cart(cart.id)
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise

        updated_cart = await self.cart_repo.get_by_user_id(user_id)
        assert updated_cart is not None
        return updated_cart
