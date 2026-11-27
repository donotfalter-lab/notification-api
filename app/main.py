import math
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.cache import redis_client
from app.db import engine
from app.routers import admin, customers, health, notifications
from app.services.limits import LimitExceeded


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await redis_client.aclose()
    await engine.dispose()


app = FastAPI(title="Notification API", version="0.1.0", lifespan=lifespan)


@app.exception_handler(LimitExceeded)
async def limit_exceeded_handler(request: Request, exc: LimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"error": exc.error, "retry_after_seconds": exc.retry_after_seconds},
        headers={**exc.headers, "Retry-After": str(max(1, math.ceil(exc.retry_after_seconds)))},
    )


@app.middleware("http")
async def add_rate_limit_headers(request: Request, call_next):
    """Copy X-RateLimit-* (set by the rate limit dependency) onto every response,
    including 404s and 422s raised after the limit check."""
    response = await call_next(request)
    for name, value in getattr(request.state, "rate_limit_headers", {}).items():
        response.headers[name] = value
    return response


app.include_router(health.router)
app.include_router(customers.router)
app.include_router(notifications.router)
app.include_router(admin.router)
