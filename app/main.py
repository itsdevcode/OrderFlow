from fastapi import FastAPI
from sqlalchemy import text
from app.core.config import settings
from app.dependencies.database import DbSession


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Production-grade Order & Inventory Management API"
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