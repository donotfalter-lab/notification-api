from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.cache import redis_client
from app.db import engine
from app.routers import admin, customers, health


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await redis_client.aclose()
    await engine.dispose()


app = FastAPI(title="Notification API", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(customers.router)
app.include_router(admin.router)
