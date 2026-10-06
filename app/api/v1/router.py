from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.categories import router as categories_router
from app.api.v1.products import router as products_router
from app.api.v1.users import router as users_router
from app.api.v1.warehouses import router as warehouses_router
from app.api.v1.inventory import router as inventory_router
from app.api.v1.cart import router as cart_router

api_router = APIRouter()

api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(categories_router)
api_router.include_router(products_router)
api_router.include_router(warehouses_router)
api_router.include_router(inventory_router)
api_router.include_router(cart_router)