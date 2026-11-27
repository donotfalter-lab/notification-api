"""Background email worker.

Run:  python -m worker.main   (docker compose runs this as the `worker` service)

- Claims jobs with BLMOVE into its own processing list (reliable queue).
- Runs up to WORKER_CONCURRENCY jobs at once (asyncio.Semaphore).
- Failed sends retry with exponential backoff via a Redis sorted set; after
  WORKER_MAX_ATTEMPTS the notification is marked failed and dead-lettered.
- Heartbeats so other workers can recover its jobs if it crashes.
- On SIGTERM/SIGINT: stops claiming, finishes in-flight jobs, then exits.
"""

import asyncio
import logging
import os
import random
import signal
import socket
import time
import uuid
from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import WorkerSettings, get_worker_settings
from app.models import Customer, Notification
from app.services import queue
from worker.email import Sender, SmtpSender, render_body

logger = logging.getLogger("worker")

CLAIM_TIMEOUT_SECONDS = 1  # how long BLMOVE blocks; also bounds shutdown latency
PROMOTE_INTERVAL_SECONDS = 0.5  # how often delayed retries are checked
HEARTBEAT_INTERVAL_SECONDS = 10
HEARTBEAT_TTL_SECONDS = 30  # a worker silent this long is considered dead
CRASH_RETRY_SECONDS = 5  # retry delay after an unexpected (non-send) error


class Worker:
    def __init__(
        self,
        *,
        settings: WorkerSettings,
        redis: Redis,
        sessions: async_sessionmaker[AsyncSession],
        send: Sender,
        worker_id: str | None = None,
    ):
        self.settings = settings
        self.redis = redis
        self.sessions = sessions
        self.send = send
        self.id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.stopping = asyncio.Event()
        self._slots = asyncio.Semaphore(settings.worker_concurrency)
        self._in_flight: set[asyncio.Task] = set()

    # --- one job ---------------------------------------------------------------

    def retry_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter: base * 2^(attempt-1), +/-20%, capped."""
        s = self.settings
        delay = min(s.worker_retry_base_seconds * 2 ** (attempt - 1), s.worker_retry_max_seconds)
        return delay * random.uniform(0.8, 1.2)

    async def process(self, job_id: str) -> None:
        try:
            notification_id = uuid.UUID(job_id)
        except ValueError:
            logger.error("job=%s dropped: not a notification id", job_id)
            await queue.ack(self.redis, self.id, job_id)
            return

        async with self.sessions() as session, session.begin():
            # Lock the row so a duplicate copy of this job (e.g. one recovered from a
            # worker that was presumed dead) can't be sent twice at the same time.
            row = (
                await session.execute(
                    select(Notification, Customer.email, Customer.name)
                    .join(Customer, Customer.id == Notification.customer_id)
                    .where(Notification.id == notification_id)
                    .with_for_update(of=Notification, skip_locked=True)
                )
            ).one_or_none()

            if row is None:
                logger.warning("job=%s skipped: notification missing or locked by another worker", job_id)
                outcome = "skip"
            else:
                notification, email, name = row
                if notification.status != "queued":
                    logger.info("job=%s skipped: already %s", job_id, notification.status)
                    outcome = "skip"
                else:
                    outcome = await self._attempt(notification, email, name)
                    attempt = notification.attempts
                    error = notification.last_error
        # Committed. Only now update Redis, so a crash in between just re-runs the job.

        if outcome in ("skip", "sent"):
            await queue.ack(self.redis, self.id, job_id)
        elif outcome == "retry":
            delay = self.retry_delay(attempt)
            await queue.schedule_retry(self.redis, self.id, job_id, time.time() + delay)
            logger.warning(
                "job=%s retry: attempt %d/%d failed (%s); next try in %.1fs",
                job_id, attempt, self.settings.worker_max_attempts, error, delay,
            )
        else:  # "dead"
            await queue.dead_letter(self.redis, self.id, job_id)
            logger.error(
                "job=%s failed: giving up after %d attempts (%s); moved to dead-letter",
                job_id, attempt, error,
            )

    async def _attempt(self, notification: Notification, email: str, name: str) -> str:
        notification.attempts += 1
        logger.info(
            "job=%s picked up: tenant=%s to=%s attempt %d/%d",
            notification.id, notification.tenant_id, email,
            notification.attempts, self.settings.worker_max_attempts,
        )
        try:
            await self.send(to=email, subject=notification.subject, text=render_body(name, notification.body))
        except Exception as exc:  # any send failure is retryable
            notification.last_error = f"{type(exc).__name__}: {exc}"[:1000]
            if notification.attempts >= self.settings.worker_max_attempts:
                notification.status = "failed"
                return "dead"
            return "retry"

        notification.status = "sent"
        notification.sent_at = datetime.now(UTC)
        notification.last_error = None
        logger.info("job=%s sent: to=%s attempt %d", notification.id, email, notification.attempts)
        return "sent"

    # --- loops -------------------------------------------------------------------

    async def _run_job(self, job_id: str) -> None:
        try:
            await self.process(job_id)
        except Exception:
            # Unexpected (e.g. DB down mid-job): nothing was committed, so try again
            # shortly. If Redis is down too, the job stays in our processing list and
            # is requeued when this worker shuts down or is reaped.
            logger.exception("job=%s crashed; retrying in %ds", job_id, CRASH_RETRY_SECONDS)
            try:
                await queue.schedule_retry(self.redis, self.id, job_id, time.time() + CRASH_RETRY_SECONDS)
            except RedisError:
                logger.error("job=%s left in processing list for recovery", job_id)
        finally:
            self._slots.release()

    async def consume(self) -> None:
        while not self.stopping.is_set():
            await self._slots.acquire()  # wait for a free slot before claiming
            if self.stopping.is_set():
                self._slots.release()
                break
            try:
                job_id = await queue.claim(self.redis, self.id, CLAIM_TIMEOUT_SECONDS)
            except RedisError as exc:
                self._slots.release()
                logger.error("redis error while claiming: %s; retrying in 1s", exc)
                await asyncio.sleep(1)
                continue
            if job_id is None:
                self._slots.release()
                continue
            task = asyncio.create_task(self._run_job(job_id))
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)

    async def promote_delayed(self) -> None:
        while not self.stopping.is_set():
            try:
                moved = await queue.promote_due(self.redis, time.time())
                if moved:
                    logger.info("moved %d delayed job(s) back to the queue", moved)
            except RedisError as exc:
                logger.error("redis error while promoting delayed jobs: %s", exc)
            await self._sleep(PROMOTE_INTERVAL_SECONDS)

    async def heartbeat_and_reap(self) -> None:
        while not self.stopping.is_set():
            try:
                await queue.heartbeat(self.redis, self.id, HEARTBEAT_TTL_SECONDS)
                for dead_id, moved in (await queue.reap_dead_workers(self.redis, self.id)).items():
                    logger.warning("recovered %d job(s) from dead worker %s", moved, dead_id)
            except RedisError as exc:
                logger.error("redis error during heartbeat: %s", exc)
            await self._sleep(HEARTBEAT_INTERVAL_SECONDS)

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake immediately on shutdown."""
        try:
            await asyncio.wait_for(self.stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def run(self) -> None:
        await queue.register_worker(self.redis, self.id, HEARTBEAT_TTL_SECONDS)
        logger.info(
            "worker %s started: concurrency=%d max_attempts=%d smtp=%s:%d delay<=%dms fail_rate=%.2f",
            self.id, self.settings.worker_concurrency, self.settings.worker_max_attempts,
            self.settings.smtp_host, self.settings.smtp_port,
            self.settings.smtp_delay_ms, self.settings.smtp_fail_rate,
        )
        background = [
            asyncio.create_task(self.promote_delayed()),
            asyncio.create_task(self.heartbeat_and_reap()),
        ]
        await self.consume()  # returns once stopping is set

        if self._in_flight:
            logger.info("shutting down: waiting for %d in-flight job(s)", len(self._in_flight))
            done, pending = await asyncio.wait(
                self._in_flight, timeout=self.settings.worker_shutdown_timeout_seconds
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                logger.warning("%d job(s) did not finish in time; they will be retried", len(pending))
        await asyncio.gather(*background, return_exceptions=True)

        moved = await queue.unregister_worker(self.redis, self.id)
        if moved:
            logger.info("returned %d unfinished job(s) to the queue", moved)
        logger.info("worker %s stopped", self.id)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_worker_settings()

    # Separate client from the API's: BLMOVE blocks, so the socket timeout must be
    # longer than the claim timeout.
    redis = Redis.from_url(
        settings.redis_url, decode_responses=True, socket_timeout=CLAIM_TIMEOUT_SECONDS + 5
    )
    # One DB connection per concurrent job.
    engine = create_async_engine(
        settings.database_url, pool_size=settings.worker_concurrency, max_overflow=5, pool_pre_ping=True
    )
    worker = Worker(
        settings=settings,
        redis=redis,
        sessions=async_sessionmaker(engine, expire_on_commit=False),
        send=SmtpSender(settings),
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_stop, worker, sig)

    try:
        await worker.run()
    finally:
        await redis.aclose()
        await engine.dispose()


def _request_stop(worker: Worker, sig: signal.Signals) -> None:
    if not worker.stopping.is_set():
        logger.info("received %s: finishing in-flight jobs, then exiting", sig.name)
        worker.stopping.set()


if __name__ == "__main__":
    asyncio.run(main())
