import asyncio
import json
import uuid

from sqlalchemy import text

from app.config import LOADTEST_DAILY_EMAIL_QUOTA, get_settings
from scripts.seed import CUSTOMER_IDS_PER_TENANT, TENANTS, seed


def _insert(api, tenant_id, customer_id, *, created_ago_s, latency_ms=None, status=None):
    """Insert a notification created `created_ago_s` seconds ago, sent `latency_ms` later."""
    status = status or ("sent" if latency_ms is not None else "queued")

    async def go():
        async with api.engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO notifications (id, tenant_id, customer_id, subject, body, status, created_at, sent_at)
                    VALUES (:id, :tenant_id, :customer_id, 's', 'b', :status,
                            now() - make_interval(secs => :ago),
                            CASE WHEN CAST(:latency AS float) IS NULL THEN NULL
                                 ELSE now() - make_interval(secs => :ago) + make_interval(secs => CAST(:latency AS float) / 1000)
                            END)
                    """
                ),
                {
                    "id": uuid.uuid4(), "tenant_id": uuid.UUID(tenant_id), "customer_id": uuid.UUID(customer_id),
                    "status": status, "ago": created_ago_s, "latency": latency_ms,
                },
            )

    asyncio.run(go())


# --- GET /admin/stats/delivery --------------------------------------------------------


def test_delivery_stats_per_tenant_percentiles(api):
    a = api.create_tenant("tenant-a", plan="pro")
    b = api.create_tenant("tenant-b")
    ca = api.add_customer(a["id"], "a@example.com")
    cb = api.add_customer(b["id"], "b@example.com")

    for latency in (100, 200, 300, 400):
        _insert(api, a["id"], ca, created_ago_s=60, latency_ms=latency)
    _insert(api, a["id"], ca, created_ago_s=30)  # still queued
    _insert(api, a["id"], ca, created_ago_s=30, status="failed")
    _insert(api, a["id"], ca, created_ago_s=600, latency_ms=99_000)  # 10 min ago: outside the window
    _insert(api, b["id"], cb, created_ago_s=10, latency_ms=1_000)

    r = api.client.get("/admin/stats/delivery", headers=api.admin)

    assert r.status_code == 200
    body = r.json()
    assert body["window_minutes"] == 5
    by_name = {t["tenant"]: t for t in body["tenants"]}

    ta = by_name["tenant-a"]
    assert (ta["plan"], ta["created"], ta["sent"], ta["pending"], ta["failed"]) == ("pro", 6, 4, 1, 1)
    # percentile_cont interpolates: p50 of 100..400 = 250, p95 = 385, p99 = 397.
    assert ta["latency_ms"] == {"p50": 250.0, "p95": 385.0, "p99": 397.0, "max": 400.0}

    tb = by_name["tenant-b"]
    assert tb["created"] == tb["sent"] == 1
    assert tb["latency_ms"] == {"p50": 1000.0, "p95": 1000.0, "p99": 1000.0, "max": 1000.0}


def test_delivery_stats_window_is_configurable(api):
    a = api.create_tenant("tenant-a")
    ca = api.add_customer(a["id"], "a@example.com")
    _insert(api, a["id"], ca, created_ago_s=600, latency_ms=50)

    assert api.client.get("/admin/stats/delivery", headers=api.admin).json()["tenants"] == []
    wide = api.client.get("/admin/stats/delivery?minutes=15", headers=api.admin).json()
    assert wide["window_minutes"] == 15 and wide["tenants"][0]["sent"] == 1


def test_delivery_stats_since_overrides_minutes(api):
    a = api.create_tenant("tenant-a")
    ca = api.add_customer(a["id"], "a@example.com")
    _insert(api, a["id"], ca, created_ago_s=120, latency_ms=50)  # before "since"
    _insert(api, a["id"], ca, created_ago_s=10, latency_ms=70)  # after

    async def db_now():
        async with api.engine.connect() as conn:
            return await conn.scalar(text("SELECT now() - interval '60 seconds'"))

    since = asyncio.run(db_now())
    r = api.client.get("/admin/stats/delivery", params={"since": since.isoformat()}, headers=api.admin)

    body = r.json()
    assert r.status_code == 200 and body["since"] is not None
    assert body["tenants"][0]["created"] == 1
    assert body["tenants"][0]["latency_ms"]["max"] == 70.0


def test_delivery_stats_with_nothing_sent_yet_has_null_latency(api):
    a = api.create_tenant("tenant-a")
    _insert(api, a["id"], api.add_customer(a["id"], "a@example.com"), created_ago_s=5)

    t = api.client.get("/admin/stats/delivery", headers=api.admin).json()["tenants"][0]
    assert t["pending"] == 1
    assert t["latency_ms"] == {"p50": None, "p95": None, "p99": None, "max": None}


def test_delivery_stats_requires_admin_token(api):
    assert api.client.get("/admin/stats/delivery").status_code == 401


# --- LOADTEST_MODE ------------------------------------------------------------------------


def test_loadtest_mode_raises_every_daily_quota(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "loadtest_mode", False)
    assert settings.plan_limits("free").daily_email_quota == settings.daily_email_quota_free

    monkeypatch.setattr(settings, "loadtest_mode", True)
    for plan in ("free", "pro"):
        limits = settings.plan_limits(plan)
        assert limits.daily_email_quota == LOADTEST_DAILY_EMAIL_QUOTA == 10_000_000
        # Rate limits are untouched.
        assert limits.burst == getattr(settings, f"rate_limit_{plan}_burst")


def test_loadtest_mode_lets_requests_past_a_used_up_quota(api, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "daily_email_quota_free", 1)
    tenant, headers = api.tenant_with_key("tenant-a")
    customer = api.add_customer(tenant["id"], "a@example.com")
    body = {"customer_id": customer, "subject": "s", "body": "b"}

    assert api.client.post("/v1/notifications", json=body, headers=headers).status_code == 202
    assert api.client.post("/v1/notifications", json=body, headers=headers).status_code == 429

    monkeypatch.setattr(settings, "loadtest_mode", True)
    assert api.client.post("/v1/notifications", json=body, headers=headers).status_code == 202


# --- seed writes customer ids for k6 -------------------------------------------------------


def test_seed_writes_keys_and_customer_ids_for_load_tests(api, tmp_path):
    keys_file = tmp_path / "loadtests" / "keys.json"

    entries = asyncio.run(seed(engine=api.engine, keys_file=keys_file, customers_per_tenant=25))

    assert json.loads(keys_file.read_text()) == entries
    assert [e["tenant"] for e in entries] == [name for name, _ in TENANTS]
    for entry in entries:
        assert len(entry["customer_ids"]) == CUSTOMER_IDS_PER_TENANT == 10
        # The key works and the customer ids belong to that tenant.
        headers = {"X-API-Key": entry["api_key"]}
        for customer_id in entry["customer_ids"][:2]:
            r = api.client.get(f"/v1/customers/{customer_id}", headers=headers)
            assert r.status_code == 200 and r.json()["tenant_id"] == entry["tenant_id"]

    # Re-running reuses the same keys and picks the same customers.
    again = asyncio.run(seed(engine=api.engine, keys_file=keys_file, customers_per_tenant=25))
    assert again == entries
