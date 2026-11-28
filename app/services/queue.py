"""Redis job queue for notifications (shared by the API and the worker).

Data layout (all values are notification ids):

    notifications:queue                 LIST   ready jobs. API LPUSHes, workers pop from the right (FIFO).
    notifications:processing:<worker>   LIST   jobs a worker has claimed (BLMOVE) but not finished.
    notifications:delayed               ZSET   jobs waiting to be retried; score = unix time when ready.
    notifications:dead                  LIST   jobs that failed every attempt.
    notifications:workers               SET    ids of registered workers.
    notifications:worker:<worker>       STRING heartbeat with a TTL; missing = worker is dead.

Reliable queue: a job is only removed from a worker's processing list once it is
finished (sent, scheduled for retry, or dead-lettered). If a worker crashes, its
heartbeat expires and another worker moves its processing list back to the queue.
"""

import uuid

from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification

QUEUE = "notifications:queue"
DELAYED = "notifications:delayed"
DEAD = "notifications:dead"
WORKERS = "notifications:workers"


def processing_key(worker_id: str) -> str:
    return f"notifications:processing:{worker_id}"


def heartbeat_key(worker_id: str) -> str:
    return f"notifications:worker:{worker_id}"


# Atomically move up to ARGV[2] jobs whose retry time (ARGV[1]) has passed back to the queue.
_PROMOTE_DUE_LUA = """
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, tonumber(ARGV[2]))
for _, id in ipairs(due) do
    redis.call('ZREM', KEYS[1], id)
    redis.call('LPUSH', KEYS[2], id)
end
return #due
"""


# --- API side --------------------------------------------------------------------


async def enqueue(redis: Redis, *notification_ids: uuid.UUID) -> None:
    if notification_ids:
        await redis.lpush(QUEUE, *(str(i) for i in notification_ids))


async def stats(redis: Redis) -> dict[str, int]:
    workers = await redis.smembers(WORKERS)
    pipe = redis.pipeline(transaction=False)
    pipe.llen(QUEUE)
    pipe.zcard(DELAYED)
    pipe.llen(DEAD)
    for worker_id in workers:
        pipe.llen(processing_key(worker_id))
    queued, delayed, dead, *processing = await pipe.execute()
    return {
        "queued": queued,
        "processing": sum(processing),
        "delayed": delayed,
        "dead_letter": dead,
        "workers": len(workers),
    }


async def replay_dead_letter(session: AsyncSession, redis: Redis) -> int:
    """Move every dead-lettered job back to the queue with a fresh set of attempts."""
    ids = await redis.lrange(DEAD, 0, -1)
    if not ids:
        return 0

    # Reset the rows first, so the worker treats them as new jobs.
    await session.execute(
        update(Notification)
        .where(Notification.id.in_([uuid.UUID(i) for i in ids]))
        .values(status="queued", attempts=0)
    )
    await session.commit()

    # New dead jobs are LPUSHed to the head while we work, so the ones we read are
    # the last len(ids) items: trim exactly those off the tail, atomically.
    pipe = redis.pipeline(transaction=True)
    pipe.lpush(QUEUE, *ids)
    pipe.ltrim(DEAD, 0, -(len(ids) + 1))
    await pipe.execute()
    return len(ids)


# --- worker side -----------------------------------------------------------------


async def claim(redis: Redis, worker_id: str, timeout: float) -> str | None:
    """Block up to `timeout` seconds for a job; move it to this worker's processing list."""
    return await redis.blmove(QUEUE, processing_key(worker_id), timeout, "RIGHT", "LEFT")


async def ack(redis: Redis, worker_id: str, job_id: str) -> None:
    await redis.lrem(processing_key(worker_id), 1, job_id)


async def schedule_retry(redis: Redis, worker_id: str, job_id: str, ready_at: float) -> None:
    pipe = redis.pipeline(transaction=True)
    pipe.zadd(DELAYED, {job_id: ready_at})
    pipe.lrem(processing_key(worker_id), 1, job_id)
    await pipe.execute()


async def dead_letter(redis: Redis, worker_id: str, job_id: str) -> None:
    pipe = redis.pipeline(transaction=True)
    pipe.lpush(DEAD, job_id)
    pipe.lrem(processing_key(worker_id), 1, job_id)
    await pipe.execute()


async def promote_due(redis: Redis, now: float, limit: int = 500) -> int:
    return await redis.eval(_PROMOTE_DUE_LUA, 2, DELAYED, QUEUE, now, limit)


async def register_worker(redis: Redis, worker_id: str, ttl_seconds: int) -> None:
    pipe = redis.pipeline(transaction=True)
    pipe.sadd(WORKERS, worker_id)
    pipe.set(heartbeat_key(worker_id), "1", ex=ttl_seconds)
    await pipe.execute()


async def heartbeat(redis: Redis, worker_id: str, ttl_seconds: int) -> bool:
    """Refresh the heartbeat and (re-)register. Returns True if the worker had to be
    added back to the registry: another worker presumed it dead (e.g. after the
    machine slept and heartbeats expired) or Redis lost the set."""
    pipe = redis.pipeline(transaction=True)
    pipe.sadd(WORKERS, worker_id)
    pipe.set(heartbeat_key(worker_id), "1", ex=ttl_seconds)
    added, _ = await pipe.execute()
    return bool(added)


async def requeue_processing(redis: Redis, worker_id: str) -> int:
    """Move every job in a worker's processing list back to the front of the queue."""
    moved = 0
    # RIGHT -> RIGHT: recovered jobs are picked up next, before newer ones.
    while await redis.lmove(processing_key(worker_id), QUEUE, "RIGHT", "RIGHT") is not None:
        moved += 1
    return moved


async def unregister_worker(redis: Redis, worker_id: str) -> int:
    """Return any unfinished jobs to the queue and forget the worker."""
    moved = await requeue_processing(redis, worker_id)
    pipe = redis.pipeline(transaction=True)
    pipe.srem(WORKERS, worker_id)
    pipe.delete(heartbeat_key(worker_id), processing_key(worker_id))
    await pipe.execute()
    return moved


async def reap_dead_workers(redis: Redis, self_id: str) -> dict[str, int]:
    """Recover jobs from workers whose heartbeat expired. Returns {worker_id: jobs_moved}."""
    recovered = {}
    for worker_id in await redis.smembers(WORKERS):
        if worker_id == self_id or await redis.exists(heartbeat_key(worker_id)):
            continue
        recovered[worker_id] = await unregister_worker(redis, worker_id)
    return recovered
