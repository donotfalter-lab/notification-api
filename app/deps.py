"""Auth dependencies: tenant API keys for /v1, admin bearer token for /admin."""

import secrets

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import get_redis
from app.config import get_settings
from app.db import get_session
from app.services.api_keys import CurrentTenant, authenticate

# auto_error=False so we control the error: same 401 for missing and invalid keys.
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
admin_bearer = HTTPBearer(auto_error=False)

INVALID_API_KEY = "Invalid or missing API key"


async def get_current_tenant(
    raw_key: str | None = Security(api_key_header),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> CurrentTenant:
    tenant = await authenticate(session, redis, raw_key)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_API_KEY)
    return tenant


async def require_admin(
    credentials: HTTPAuthorizationCredentials | None = Security(admin_bearer),
) -> None:
    expected = get_settings().admin_token.get_secret_value().encode()
    if credentials is None or not secrets.compare_digest(credentials.credentials.encode(), expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin token",
            headers={"WWW-Authenticate": "Bearer"},
        )
