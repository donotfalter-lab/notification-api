"""API key generation, lookup (with Redis cache), and revocation."""

import hashlib
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import ApiKey, Tenant

logger = logging.getLogger(__name__)

API_KEY_PREFIX = "ntf_live_"
API_KEY_RANDOM_LENGTH = 32
API_KEY_LENGTH = len(API_KEY_PREFIX) + API_KEY_RANDOM_LENGTH
DISPLAY_PREFIX_LENGTH = 12
CACHE_KEY_PREFIX = "auth:apikey:"


@dataclass(frozen=True)
class CurrentTenant:
    """The tenant an authenticated request belongs to."""

    id: uuid.UUID
    name: str
    plan: str
    api_key_id: uuid.UUID

    def to_json(self) -> str:
        return json.dumps(
            {"id": str(self.id), "name": self.name, "plan": self.plan, "api_key_id": str(self.api_key_id)}
        )

    @classmethod
    def from_json(cls, raw: str) -> "CurrentTenant":
        data = json.loads(raw)
        return cls(
            id=uuid.UUID(data["id"]),
            name=data["name"],
            plan=data["plan"],
            api_key_id=uuid.UUID(data["api_key_id"]),
        )


def generate_api_key() -> str:
    # 24 random bytes -> exactly 32 url-safe base64 characters.
    return API_KEY_PREFIX + secrets.token_urlsafe(24)


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _cache_key(key_hash: str) -> str:
    return CACHE_KEY_PREFIX + key_hash


async def create_api_key(
    session: AsyncSession, tenant_id: uuid.UUID, name: str
) -> tuple[ApiKey, str]:
    """Create a key. Returns the stored row and the raw key (only time it exists)."""
    raw_key = generate_api_key()
    api_key = ApiKey(
        tenant_id=tenant_id,
        name=name,
        key_prefix=raw_key[:DISPLAY_PREFIX_LENGTH],
        key_hash=hash_api_key(raw_key),
    )
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)
    return api_key, raw_key


async def authenticate(
    session: AsyncSession, redis: Redis, raw_key: str | None
) -> CurrentTenant | None:
    """Return the tenant for a valid, non-revoked key, or None."""
    if not raw_key or len(raw_key) != API_KEY_LENGTH or not raw_key.startswith(API_KEY_PREFIX):
        return None

    key_hash = hash_api_key(raw_key)
    cache_key = _cache_key(key_hash)

    try:
        cached = await redis.get(cache_key)
    except RedisError:
        logger.warning("Redis unavailable for API key lookup; falling back to Postgres")
        cached = None
    if cached:
        return CurrentTenant.from_json(cached)

    row = (
        await session.execute(
            select(ApiKey.id, Tenant.id, Tenant.name, Tenant.plan)
            .join(Tenant, Tenant.id == ApiKey.tenant_id)
            .where(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None))
        )
    ).one_or_none()
    if row is None:
        return None

    api_key_id, tenant_id, tenant_name, plan = row
    current = CurrentTenant(id=tenant_id, name=tenant_name, plan=plan, api_key_id=api_key_id)

    try:
        await redis.set(cache_key, current.to_json(), ex=get_settings().api_key_cache_ttl_seconds)
    except RedisError:
        logger.warning("Redis unavailable; API key lookup not cached")
    return current


async def revoke_api_key(
    session: AsyncSession, redis: Redis, api_key_id: uuid.UUID
) -> ApiKey | None:
    """Revoke a key and drop it from the cache so it stops working immediately."""
    api_key = await session.get(ApiKey, api_key_id)
    if api_key is None:
        return None

    if api_key.revoked_at is None:
        api_key.revoked_at = datetime.now(UTC)
        await session.commit()

    # Delete after the commit so a concurrent request can't re-cache the old state.
    try:
        await redis.delete(_cache_key(api_key.key_hash))
    except RedisError:
        logger.warning(
            "Redis unavailable; revoked key %s may keep working until its cache entry expires",
            api_key.id,
        )
    return api_key
