from redis.asyncio import Redis

from app.config import get_settings

# Short timeouts: if Redis is slow or down, auth falls back to Postgres quickly
# instead of hanging the request.
redis_client: Redis = Redis.from_url(
    get_settings().redis_url,
    decode_responses=True,
    socket_timeout=0.5,
    socket_connect_timeout=0.5,
)


def get_redis() -> Redis:
    """FastAPI dependency (overridable in tests)."""
    return redis_client
