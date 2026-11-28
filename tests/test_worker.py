import asyncio
import time

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_worker_settings
from app.services import queue
from worker.email import first_name, render_body
from worker.main import Worker


class RecordingSender:
    """Stands in for SMTP: records emails, optionally fails, optionally waits for a signal."""

    def __init__(self, fail: bool = False, gate: asyncio.Event | None = None):
        self.fail = fail
        self.gate = gate
        self.started = asyncio.Event()
        self.sent: list[dict] = []

    async def __call__(self, *, to: str, subject: str, text: str) -> None:
        self.started.set()
        if self.gate:
            await self.gate.wait()
        if self.fail:
            raise ConnectionError("smtp down")
        self.sent.append({"to": to, "subject": subject, "text": text})


def make_worker(api, redis, sender, **overrides) -> Worker:
    settings = get_worker_settings().model_copy(update={"worker_retry_base_seconds": 1, **overrides})
    return Worker(
        settings=settings,
        redis=redis,
        sessions=async_sessionmaker(api.engine, expire_on_commit=False),
        send=sender,
        worker_id="test-worker",
    )


def queue_one(api, name="Ada Lovelace") -> str:
    tenant, headers = api.tenant_with_key("tenant-a")
    customer_id = api.add_customer(tenant["id"], "ada@example.com", name=name)
    r = api.client.post(
        "/v1/notifications",
        json={"customer_id": customer_id, "subject": "Hello", "body": "Your report is ready."},
        headers=headers,
    )
    assert r.status_code == 202
    return r.json()["id"]


def claim_and_process(api, sender, **overrides) -> str:
    """Claim the next job the way the worker does (BLMOVE), then process it."""

    async def go(redis):
        worker = make_worker(api, redis, sender, **overrides)
        job_id = await queue.claim(redis, worker.id, 0.1)
        assert job_id is not None
        await worker.process(job_id)
        return job_id

    return api.run(go)


def processing(api) -> list[str]:
    return api.run(lambda r: r.lrange(queue.processing_key("test-worker"), 0, -1))


# --- email content -----------------------------------------------------------------


@pytest.mark.parametrize(
    "name, expected",
    [("Ada Lovelace", "Ada"), ("  Grace   Hopper ", "Grace"), ("Cher", "Cher"), ("", "there"), ("   ", "there")],
)
def test_first_name(name, expected):
    assert first_name(name) == expected


def test_render_body_starts_with_greeting():
    assert render_body("Ada Lovelace", "Body text").startswith("Hi Ada,\n\nBody text")


# --- processing outcomes -------------------------------------------------------------


def test_successful_send_marks_sent_and_acks(api):
    notification_id = queue_one(api)
    sender = RecordingSender()

    claim_and_process(api, sender)

    assert sender.sent == [
        {"to": "ada@example.com", "subject": "Hello", "text": "Hi Ada,\n\nYour report is ready.\n"}
    ]
    row = api.notification_rows()[0]
    assert row["status"] == "sent" and row["attempts"] == 1 and row["sent_at"] is not None
    assert processing(api) == [] and api.queued_ids() == []
    assert str(row["id"]) == notification_id


def test_failed_send_schedules_retry_with_backoff(api):
    notification_id = queue_one(api)
    before = time.time()

    claim_and_process(api, RecordingSender(fail=True))

    row = api.notification_rows()[0]
    assert row["status"] == "queued" and row["attempts"] == 1
    assert "smtp down" in row["last_error"]
    score = api.run(lambda r: r.zscore(queue.DELAYED, notification_id))
    assert before + 0.8 <= score <= time.time() + 1.2  # base 1s, +/-20% jitter
    assert processing(api) == [] and api.queued_ids() == []


def test_backoff_is_exponential_and_capped(api):
    async def go(redis):
        return make_worker(api, redis, RecordingSender(), worker_retry_max_seconds=10)

    worker = api.run(go)
    assert [round(worker.retry_delay(n) / (2 ** (n - 1)), 1) for n in (1, 2, 3)] == pytest.approx([1, 1, 1], abs=0.2)
    assert worker.retry_delay(10) <= 10 * 1.2


def test_last_attempt_failure_marks_failed_and_dead_letters(api):
    notification_id = queue_one(api)

    claim_and_process(api, RecordingSender(fail=True), worker_max_attempts=1)

    row = api.notification_rows()[0]
    assert row["status"] == "failed" and row["attempts"] == 1
    assert api.run(lambda r: r.lrange(queue.DEAD, 0, -1)) == [notification_id]
    assert processing(api) == []


def test_already_sent_job_is_skipped(api):
    queue_one(api)
    claim_and_process(api, RecordingSender())

    # A duplicate copy of the same job arrives (e.g. recovered from a crashed worker).
    job_id = api.notification_rows()[0]["id"]
    api.run(lambda r: queue.enqueue(r, job_id))
    sender = RecordingSender()
    claim_and_process(api, sender)

    assert sender.sent == []  # not sent twice
    assert processing(api) == []


# --- delayed retries, crash recovery, shutdown -------------------------------------


def test_due_delayed_jobs_move_back_to_queue(api):
    async def go(redis):
        await redis.zadd(queue.DELAYED, {"due": time.time() - 1, "later": time.time() + 60})
        moved = await queue.promote_due(redis, time.time())
        return moved, await redis.lrange(queue.QUEUE, 0, -1), await redis.zrange(queue.DELAYED, 0, -1)

    moved, queued, delayed = api.run(go)
    assert moved == 1 and queued == ["due"] and delayed == ["later"]


def test_jobs_of_a_crashed_worker_are_recovered(api):
    async def go(redis):
        # A worker claimed two jobs, then died: registered, but its heartbeat expired.
        await redis.sadd(queue.WORKERS, "crashed")
        await redis.lpush(queue.processing_key("crashed"), "job-1", "job-2")
        recovered = await queue.reap_dead_workers(redis, self_id="test-worker")
        return recovered, await redis.lrange(queue.QUEUE, 0, -1), await redis.smembers(queue.WORKERS)

    recovered, queued, workers = api.run(go)
    assert recovered == {"crashed": 2}
    assert sorted(queued) == ["job-1", "job-2"]
    assert "crashed" not in workers


@pytest.fixture
def blocking_claim(monkeypatch):
    """fakeredis's BLMOVE returns immediately instead of blocking like real Redis,
    which would make the worker's claim loop spin. Emulate the real blocking wait."""
    real_claim = queue.claim

    async def claim(redis, worker_id, timeout):
        job_id = await real_claim(redis, worker_id, timeout)
        if job_id is None:
            await asyncio.sleep(min(timeout, 0.05))
        return job_id

    monkeypatch.setattr(queue, "claim", claim)


def test_live_worker_wrongly_reaped_re_registers_on_next_heartbeat(api):
    async def go(redis):
        await queue.register_worker(redis, "alive", ttl_seconds=30)
        # Its heartbeat lapsed (e.g. the machine slept) and another worker reaped it.
        await redis.delete(queue.heartbeat_key("alive"))
        await queue.reap_dead_workers(redis, self_id="other")
        reaped = await queue.stats(redis)

        re_added = await queue.heartbeat(redis, "alive", ttl_seconds=30)
        healed = await queue.stats(redis)
        again = await queue.heartbeat(redis, "alive", ttl_seconds=30)
        return reaped["workers"], re_added, healed["workers"], again

    reaped, re_added, healed, again = api.run(go)
    assert reaped == 0  # this was the bug: stuck at 0 while the worker kept running
    assert re_added is True and healed == 1
    assert again is False  # normal heartbeats don't report a re-registration


def test_shutdown_finishes_in_flight_jobs(api, blocking_claim):
    queue_one(api)

    async def go(redis):
        gate = asyncio.Event()
        sender = RecordingSender(gate=gate)
        worker = make_worker(api, redis, sender)
        run = asyncio.create_task(worker.run())

        await asyncio.wait_for(sender.started.wait(), timeout=5)  # job is mid-send
        worker.stopping.set()  # what SIGTERM does
        await asyncio.sleep(0.1)
        assert not run.done()  # still waiting for the in-flight job
        gate.set()  # let the send finish
        await asyncio.wait_for(run, timeout=5)
        return sender, await redis.smembers(queue.WORKERS)

    sender, workers = api.run(go)
    assert len(sender.sent) == 1
    assert api.notification_rows()[0]["status"] == "sent"
    assert workers == set()  # unregistered cleanly
    assert processing(api) == []


# --- admin queue endpoints ---------------------------------------------------------------


def test_queue_stats_and_dead_letter_replay(api):
    notification_id = queue_one(api)
    claim_and_process(api, RecordingSender(fail=True), worker_max_attempts=1)

    stats = api.client.get("/admin/queue/stats", headers=api.admin).json()
    assert stats == {"queued": 0, "processing": 0, "delayed": 0, "dead_letter": 1, "workers": 0}

    r = api.client.post("/admin/dead-letter/replay", headers=api.admin)
    assert r.status_code == 200 and r.json() == {"replayed": 1}

    assert api.queued_ids() == [notification_id]
    row = api.notification_rows()[0]
    assert row["status"] == "queued" and row["attempts"] == 0
    assert api.client.get("/admin/queue/stats", headers=api.admin).json()["dead_letter"] == 0

    # And it can now be delivered.
    sender = RecordingSender()
    claim_and_process(api, sender)
    assert len(sender.sent) == 1


def test_queue_endpoints_require_admin_token(api):
    assert api.client.get("/admin/queue/stats").status_code == 401
    assert api.client.post("/admin/dead-letter/replay").status_code == 401
