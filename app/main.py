from fastapi import FastAPI


app = FastAPI(
    title="OrderFlow API",
    description="Production-grade Order & Inventory Management API",
    version="1.0.0",
)


@app.get("/health", tags=["Health"])
async def health_check() -> dict[str, str]:
    return {"status": "ok"}