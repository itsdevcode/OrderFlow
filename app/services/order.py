from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions.cart import ProductNotActiveError
from app.exceptions.order import EmptyCartError, OrderNotFoundError
from app.exceptions.product import ProductNotFoundError
from app.models.order import Order, OrderStatus
from app.models.order_item import OrderItem
from app.repositories.cart import CartRepository
from app.repositories.order import OrderRepository
from app.repositories.product import ProductRepository


class OrderService:
    db: AsyncSession
    order_repo: OrderRepository
    cart_repo: CartRepository
    product_repo: ProductRepository

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.order_repo = OrderRepository(db)
        self.cart_repo = CartRepository(db)
        self.product_repo = ProductRepository(db)

    async def create_order_from_cart(self, user_id: int) -> Order:
        cart = await self.cart_repo.get_by_user_id(user_id)
        if not cart or not cart.items:
            raise EmptyCartError()

        order_items: list[OrderItem] = []

        # Validate products and build snapshot
        for cart_item in cart.items:
            product = await self.product_repo.get_by_id(cart_item.product_id)
            if not product:
                raise ProductNotFoundError(f"Product with id {cart_item.product_id} not found")
            if not product.is_active:
                raise ProductNotActiveError(f"Product '{product.name}' is not active")

            order_item = OrderItem(
                product_id=product.id,
                product_name=product.name,
                product_sku=product.sku,
                unit_price=product.price,
                quantity=cart_item.quantity
            )
            order_items.append(order_item)

        new_order = Order(
            user_id=user_id,
            status=OrderStatus.PENDING
        )

        try:
            # Atomic operation
            created_order = await self.order_repo.create_order(new_order, order_items)
            await self.cart_repo.clear_cart(cart.id)
            
            await self.db.commit()
            return created_order
        except Exception:
            await self.db.rollback()
            raise

    async def get_order_by_id(self, user_id: int, order_id: int) -> Order:
        order = await self.order_repo.get_by_id_and_user_id(order_id, user_id)
        if not order:
            raise OrderNotFoundError()
        return order

    async def list_orders(self, user_id: int, offset: int = 0, limit: int = 20) -> list[Order]:
        return await self.order_repo.list_by_user_id(user_id, offset, limit)
