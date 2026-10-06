from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.cart import Cart
from app.models.cart_item import CartItem


class CartRepository:
    db: AsyncSession

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_user_id(self, user_id: int) -> Cart | None:
        stmt = (
            select(Cart)
            .options(
                selectinload(Cart.items).selectinload(CartItem.product)
            )
            .where(Cart.user_id == user_id)
            .execution_options(populate_existing=True)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def create_cart(self, user_id: int) -> Cart:
        cart = Cart(user_id=user_id)
        self.db.add(cart)
        await self.db.flush()
        await self.db.refresh(cart)
        return cart

    async def get_item_by_id(self, item_id: int) -> CartItem | None:
        stmt = (
            select(CartItem)
            .options(selectinload(CartItem.product))
            .where(CartItem.id == item_id)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_item_by_cart_and_product(self, cart_id: int, product_id: int) -> CartItem | None:
        stmt = (
            select(CartItem)
            .options(selectinload(CartItem.product))
            .where(CartItem.cart_id == cart_id, CartItem.product_id == product_id)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def add_item(self, cart_id: int, product_id: int, quantity: int) -> CartItem:
        item = CartItem(cart_id=cart_id, product_id=product_id, quantity=quantity)
        self.db.add(item)
        await self.db.flush()
        await self.db.refresh(item)
        # Refresh to load product
        stmt = select(CartItem).options(selectinload(CartItem.product)).where(CartItem.id == item.id)
        result = await self.db.execute(stmt)
        return result.scalar_one()

    async def update_item(self, item: CartItem, quantity: int) -> CartItem:
        item.quantity = quantity
        await self.db.flush()
        await self.db.refresh(item)
        return item

    async def delete_item(self, item: CartItem) -> None:
        await self.db.delete(item)
        await self.db.flush()

    async def clear_cart(self, cart_id: int) -> None:
        stmt = delete(CartItem).where(CartItem.cart_id == cart_id)
        _ = await self.db.execute(stmt)
        await self.db.flush()
