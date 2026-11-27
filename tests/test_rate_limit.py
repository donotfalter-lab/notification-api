import logging
import math
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fakeredis.commands_mixins import server_mixin
from redis.asyncio import Redis

from app.cache import get_redis
from app.config import get_settings
from app.main import app
from app.services import limits


class FakeClock:
    """Stands in for the clock behind Redis's TIME command (used by the Lua token bucket)."""

    def __init__(self):
        self.now = 1_800_000_000.0

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    # Freeze time so "rapid" requests really happen at the same instant, regardless
    # of how fast the test machine is (otherwise tokens trickle back in between).
    fake = FakeClock()
    monkeypatch.setattr(server_mixin, "time", fake)
    return fake


@pytest.fixture
def quota(monkeypatch):
    """Set a small daily quota for both plans."""

    def set_quota(n: int):
        settings = get_settings()
        monkeypatch.setattr(settings, "daily_email_quota_free", n)
        monkeypatch.setattr(settings, "daily_email_quota_pro", n)

    return set_quota


def _tenant(api, name, plan="free"):
    tenant = api.create_tenant(name, plan=plan)
    return tenant, {"X-API-Key": api.create_key(tenant["id"])["api_key"]}


def _get(api, headers, path="/v1/customers?limit=1"):
    return api.client.get(path, headers=headers)


# --- rate limit -------------------------------------------------------------------------


def test_21st_rapid_request_from_free_tenant_is_429_while_pro_is_unaffected(api, clock):
    _, free = _tenant(api, "free-co", "free")
    _, pro = _tenant(api, "pro-co", "pro")

    # Free plan: burst of 20 allowed...
    assert [_get(api, free).status_code for _ in range(20)] == [200] * 20
    # ...the 21st at the same moment is rejected.
    r = _get(api, free)
    assert r.status_code == 429
    body = r.json()
    assert set(body) == {"error", "retry_after_seconds"}
    assert "Rate limit exceeded" in body["error"] and "free plan" in body["error"]
    assert body["retry_after_seconds"] == pytest.approx(0.1)  # one token at 10/s
    assert r.headers["Retry-After"] == "1"
    assert r.headers["X-RateLimit-Limit"] == "20"
    assert r.headers["X-RateLimit-Remaining"] == "0"

    # Same instant: the pro tenant has its own, bigger bucket.
    assert [_get(api, pro).status_code for _ in range(21)] == [200] * 21
    assert _get(api, pro).headers["X-RateLimit-Remaining"] == str(200 - 22)


def test_every_response_carries_rate_limit_headers(api, clock):
    tenant, headers = _tenant(api, "acme")

    ok = _get(api, headers)
    assert ok.status_code == 200
    assert ok.headers["X-RateLimit-Limit"] == "20"
    assert ok.headers["X-RateLimit-Remaining"] == "19"
    assert ok.headers["X-RateLimit-Reset"] == "1"  # 1 token to refill at 10/s = 0.1s, rounded up

    # Error responses raised after the limit check carry them too.
    not_found = _get(api, headers, "/v1/customers/00000000-0000-0000-0000-000000000000")
    assert not_found.status_code == 404
    assert not_found.headers["X-RateLimit-Remaining"] == "18"


def test_tokens_refill_over_time(api, clock):
    _, headers = _tenant(api, "acme")
    for _ in range(20):
        _get(api, headers)
    assert _get(api, headers).status_code == 429

    clock.advance(0.5)  # 0.5s * 10/s = 5 tokens back

    assert [_get(api, headers).status_code for _ in range(6)] == [200] * 5 + [429]


def test_each_tenant_has_its_own_bucket(api, clock):
    _, a = _tenant(api, "tenant-a")
    _, b = _tenant(api, "tenant-b")
    for _ in range(21):
        _get(api, a)

    assert _get(api, a).status_code == 429
    assert _get(api, b).status_code == 200


def test_rate_limit_runs_after_auth(api, clock):
    # No key: 401, not 429, and no rate-limit headers (no tenant to count against).
    r = api.client.get("/v1/customers")
    assert r.status_code == 401
    assert "X-RateLimit-Limit" not in r.headers


def test_plan_limits_come_from_config(api, clock, monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_free_burst", 3)
    _, headers = _tenant(api, "acme")

    assert [_get(api, headers).status_code for _ in range(4)] == [200, 200, 200, 429]


# --- daily email quota -------------------------------------------------------------------


def _send(api, headers, customer_id, key=None):
    h = {**headers, **({"Idempotency-Key": key} if key else {})}
    return api.client.post(
        "/v1/notifications", json={"customer_id": customer_id, "subject": "Hi", "body": "Hello"}, headers=h
    )


def _batch(api, headers, customer_id, n):
    items = [{"customer_id": customer_id, "subject": "Hi", "body": "Hello"}] * n
    return api.client.post("/v1/notifications/batch", json={"notifications": items}, headers=headers)


def test_daily_quota_counts_each_email_in_a_batch(api, quota):
    quota(5)
    tenant, headers = _tenant(api, "acme")
    customer = api.add_customer(tenant["id"], "a@example.com")

    assert _batch(api, headers, customer, 4).status_code == 202  # 4 used

    r = _batch(api, headers, customer, 2)  # would make 6
    assert r.status_code == 429
    assert "Daily email quota exceeded" in r.json()["error"]
    assert "5 emails per day" in r.json()["error"] and "1 left today" in r.json()["error"]

    assert _send(api, headers, customer).status_code == 202  # exactly 5
    over = _send(api, headers, customer)
    assert over.status_code == 429
    assert "midnight UTC" in over.json()["error"]

    # Retry-After points at midnight UTC.
    now = datetime.now(UTC)
    to_midnight = (datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC) - now).total_seconds()
    assert abs(over.json()["retry_after_seconds"] - to_midnight) <= 5
    assert int(over.headers["Retry-After"]) == math.ceil(over.json()["retry_after_seconds"])
    assert len(api.notification_rows()) == 5


def test_quota_not_used_by_failed_or_replayed_requests(api, quota):
    quota(1)
    tenant, headers = _tenant(api, "acme")
    customer = api.add_customer(tenant["id"], "a@example.com")

    # Unknown customer: rejected, quota refunded.
    assert _send(api, headers, "00000000-0000-0000-0000-000000000000").status_code == 422
    # So the one allowed email still goes through...
    first = _send(api, headers, customer, key="order-1")
    assert first.status_code == 202
    # ...and repeating it returns the original even though the quota is used up.
    again = _send(api, headers, customer, key="order-1")
    assert again.status_code == 202 and again.json()["id"] == first.json()["id"]
    # A genuinely new email is over quota.
    assert _send(api, headers, customer, key="order-2").status_code == 429


def test_quota_counter_expires_at_midnight_utc(api, quota):
    quota(10)
    tenant, headers = _tenant(api, "acme")
    customer = api.add_customer(tenant["id"], "a@example.com")
    assert _send(api, headers, customer).status_code == 202

    now = datetime.now(UTC)
    key = limits.quota_key(uuid.UUID(tenant["id"]), now)
    used, expire_at = api.run(lambda r: _get_used_and_expiry(r, key))
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    assert used == "1"
    assert expire_at == int(midnight.timestamp())


async def _get_used_and_expiry(redis, key):
    return await redis.get(key), await redis.expiretime(key)


# --- Redis down: fail open -----------------------------------------------------------------


def test_fails_open_and_logs_warning_when_redis_is_down(api, caplog, monkeypatch):
    monkeypatch.setattr(limits, "_last_warned", {})
    tenant, headers = _tenant(api, "acme")
    app.dependency_overrides[get_redis] = lambda: Redis.from_url(
        "redis://127.0.0.1:1/0", socket_connect_timeout=0.2
    )

    with caplog.at_level(logging.WARNING, logger="app.services.limits"):
        r = _get(api, headers)

    assert r.status_code == 200  # allowed, even though limits couldn't be checked
    assert "X-RateLimit-Limit" not in r.headers  # nothing to report
    assert any("rate limit not enforced (failing open)" in m for m in caplog.messages)
