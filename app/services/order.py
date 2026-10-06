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

        from datetime import datetime, timezone, timedelta
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        from app.models.inventory_reservation import InventoryReservation, ReservationStatus
        from app.exceptions.inventory import InsufficientStockError
        from app.repositories.inventory import InventoryRepository

        inventory_repo = InventoryRepository(self.db)

        # 1. Validate and Sort cart items
        sorted_items = sorted(cart.items, key=lambda x: x.product_id)

        order_items: list[OrderItem] = []
        reservation_data: list[dict[str, int]] = []

        try:
            for cart_item in sorted_items:
                product = await self.product_repo.get_by_id(cart_item.product_id)
                if not product:
                    raise ProductNotFoundError(f"Product with id {cart_item.product_id} not found")
                if not product.is_active:
                    raise ProductNotActiveError(f"Product '{product.name}' is not active")

                # Lock inventory
                inventory_rows = await inventory_repo.get_all_for_update_by_product(product.id)
                
                selected_inv = None
                for inv in inventory_rows:
                    if inv.available_quantity >= cart_item.quantity:
                        selected_inv = inv
                        break
                
                if not selected_inv:
                    raise InsufficientStockError(f"Insufficient stock for '{product.name}'")

                # Mutate stock
                selected_inv.available_quantity -= cart_item.quantity
                selected_inv.reserved_quantity += cart_item.quantity

                order_item = OrderItem(
                    product_id=product.id,
                    product_name=product.name,
                    product_sku=product.sku,
                    unit_price=product.price,
                    quantity=cart_item.quantity
                )
                order_items.append(order_item)
                
                reservation_data.append({
                    "product_id": product.id,
                    "warehouse_id": selected_inv.warehouse_id,
                    "quantity": cart_item.quantity,
                })

            new_order = Order(
                user_id=user_id,
                status=OrderStatus.PENDING
            )

            self.db.add(new_order)
            await self.db.flush()

            now = datetime.now(timezone.utc)
            expires_at = now + timedelta(minutes=15)

            for order_item, res_data in zip(order_items, reservation_data):
                order_item.order_id = new_order.id
                self.db.add(order_item)

                reservation = InventoryReservation(
                    order_id=new_order.id,
                    product_id=res_data["product_id"],
                    warehouse_id=res_data["warehouse_id"],
                    quantity=res_data["quantity"],
                    status=ReservationStatus.ACTIVE,
                    expires_at=expires_at
                )
                self.db.add(reservation)

            await self.db.flush()

            await self.cart_repo.clear_cart(cart.id)
            await self.db.commit()
            
            # Refetch with eager loading for items
            stmt = select(Order).where(Order.id == new_order.id).options(selectinload(Order.items))
            result = await self.db.execute(stmt)
            return result.scalar_one()

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
