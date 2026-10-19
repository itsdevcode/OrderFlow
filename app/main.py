from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app.core.config import settings
from app.core.redis import close_redis_client, get_redis_client, init_redis_client
from app.dependencies.database import DbSession
from app.api.v1.router import api_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await init_redis_client()
    try:
        yield
    finally:
        await close_redis_client()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Production-grade Order & Inventory Management API",
    lifespan=lifespan,
)

app.include_router(
    api_router,
    prefix="/api/v1",
)
@app.get("/health", tags=["Health"])
async def health_check() -> dict[str, str]:
    return {"status": "ok"}

@app.get("/health/db")
async def database_health_check(db: DbSession) -> dict[str, str]:
    _ = await db.execute(text("SELECT 1"))

    return {
        "status": "ok",
        "database": "connected",
    }