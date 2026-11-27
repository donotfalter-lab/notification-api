"""Per-tenant rate limiting (token bucket) and daily email quota, both in Redis.

Both checks run as Lua scripts, so "check and update" is one atomic step in
Redis: several API containers can share the same limits without races.

If Redis is unreachable, both checks FAIL OPEN: the request is allowed and a
warning is logged (at most every few seconds, so logs aren't flooded).
"""

import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import PlanLimits

logger = logging.getLogger(__name__)

WARN_EVERY_SECONDS = 10
_last_warned: dict[str, float] = {}


def _warn_fail_open(what: str, exc: Exception) -> None:
    now = time.monotonic()
    if now - _last_warned.get(what, -math.inf) >= WARN_EVERY_SECONDS:
        _last_warned[what] = now
        logger.warning("Redis unavailable, %s not enforced (failing open): %s", what, exc)


class LimitExceeded(Exception):
    """Rendered as 429 {error, retry_after_seconds} with a Retry-After header."""

    def __init__(self, error: str, retry_after_seconds: float, headers: dict[str, str] | None = None):
        super().__init__(error)
        self.error = error
        self.retry_after_seconds = retry_after_seconds
        self.headers = headers or {}


# --- rate limit: token bucket ---------------------------------------------------------

# KEYS[1] bucket hash {tokens, ts}; ARGV: rate (tokens/s), burst (capacity), cost.
# Uses Redis's own clock so every API container agrees on "now".
# Returns {allowed, tokens_left, retry_after_s, reset_s} (floats as strings: Lua
# numbers returned to Redis would be truncated to integers).
_TOKEN_BUCKET_LUA = """
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil or ts == nil then
    tokens = burst
    ts = now
end
tokens = math.min(burst, tokens + math.max(0, now - ts) * rate)

local allowed = 0
local retry_after = 0
if tokens >= cost then
    tokens = tokens - cost
    allowed = 1
else
    retry_after = (cost - tokens) / rate
end

redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
-- Drop idle buckets once they'd be full again anyway.
redis.call('PEXPIRE', KEYS[1], math.ceil(burst / rate * 1000) + 1000)
return {allowed, tostring(tokens), tostring(retry_after), tostring((burst - tokens) / rate)}
"""


def rate_limit_key(tenant_id: uuid.UUID) -> str:
    return f"ratelimit:{tenant_id}"


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int  # until the bucket is full again
    retry_after_seconds: float  # 0 when allowed

    def headers(self) -> dict[str, str]:
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_seconds),
        }


async def check_rate_limit(redis: Redis, tenant_id: uuid.UUID, limits: PlanLimits) -> RateLimitResult | None:
    """Spend one token. Returns None if Redis is down (fail open)."""
    try:
        allowed, tokens, retry_after, reset = await redis.eval(
            _TOKEN_BUCKET_LUA, 1, rate_limit_key(tenant_id), limits.rps, limits.burst, 1
        )
    except RedisError as exc:
        _warn_fail_open("rate limit", exc)
        return None
    return RateLimitResult(
        allowed=bool(int(allowed)),
        limit=limits.burst,
        remaining=max(0, math.floor(float(tokens))),
        reset_seconds=math.ceil(float(reset)),
        retry_after_seconds=round(float(retry_after), 3),
    )


# --- daily email quota ----------------------------------------------------------------

# KEYS[1] counter; ARGV: count, limit, expire-at (unix time of next UTC midnight).
# Only increments if the whole count fits, so a rejected batch uses no quota.
_QUOTA_LUA = """
local count = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local used = tonumber(redis.call('GET', KEYS[1]) or '0')
if used + count > limit then
    return {0, used}
end
used = redis.call('INCRBY', KEYS[1], count)
redis.call('EXPIREAT', KEYS[1], ARGV[3])
return {1, used}
"""


def _next_utc_midnight(now: datetime) -> datetime:
    return datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)


def quota_key(tenant_id: uuid.UUID, now: datetime) -> str:
    return f"quota:email:{tenant_id}:{now:%Y-%m-%d}"


@dataclass
class QuotaReservation:
    """Emails counted against today's quota; refund the ones that weren't created."""

    redis: Redis
    key: str | None  # None when Redis was down (nothing was counted)
    count: int
    refunded: int = field(default=0)

    async def refund(self, count: int | None = None) -> None:
        count = self.count - self.refunded if count is None else min(count, self.count - self.refunded)
        if not self.key or count <= 0:
            return
        try:
            await self.redis.decrby(self.key, count)
            self.refunded += count
        except RedisError as exc:
            _warn_fail_open("email quota refund", exc)


async def reserve_email_quota(
    redis: Redis, tenant_id: uuid.UUID, limits: PlanLimits, count: int
) -> QuotaReservation:
    """Count `count` emails against today's quota, or raise LimitExceeded."""
    now = datetime.now(UTC)
    midnight = _next_utc_midnight(now)
    key = quota_key(tenant_id, now)
    try:
        allowed, used = await redis.eval(
            _QUOTA_LUA, 1, key, count, limits.daily_email_quota, int(midnight.timestamp())
        )
    except RedisError as exc:
        _warn_fail_open("daily email quota", exc)
        return QuotaReservation(redis, key=None, count=0)

    if not int(allowed):
        left = max(0, limits.daily_email_quota - int(used))
        asked = f"this request has {count:,} emails, " if count > 1 else ""
        raise LimitExceeded(
            f"Daily email quota exceeded: the {limits.plan} plan allows "
            f"{limits.daily_email_quota:,} emails per day; {asked}{left:,} left today. "
            "The quota resets at midnight UTC.",
            retry_after_seconds=math.ceil((midnight - now).total_seconds()),
        )
    return QuotaReservation(redis, key=key, count=count)
