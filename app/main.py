from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import engine
from app.routers import customers, health


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(title="Notification API", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(customers.router)
