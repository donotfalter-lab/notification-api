import uuid

from redis.asyncio import Redis

from app.cache import get_redis
from app.main import app


def _payload(customer_id: str, subject: str = "Your order shipped") -> dict:
    return {"customer_id": customer_id, "subject": subject, "body": "It's on the way."}


# --- 202 response ----------------------------------------------------------------


def test_create_returns_202_with_id_and_status_and_enqueues(api):
    tenant, headers = api.tenant_with_key("tenant-a")
    customer_id = api.add_customer(tenant["id"], "ada@example.com")

    r = api.client.post("/v1/notifications", json=_payload(customer_id), headers=headers)

    assert r.status_code == 202
    body = r.json()
    assert set(body) == {"id", "status"}
    assert body["status"] == "queued"
    uuid.UUID(body["id"])  # a real id

    rows = api.notification_rows()
    assert len(rows) == 1 and str(rows[0]["id"]) == body["id"] and rows[0]["status"] == "queued"
    assert api.queued_ids() == [body["id"]]


def test_get_notification_returns_own_and_404s_other_tenants(api):
    a, headers_a = api.tenant_with_key("tenant-a")
    _, headers_b = api.tenant_with_key("tenant-b")
    created = api.client.post(
        "/v1/notifications", json=_payload(api.add_customer(a["id"], "ada@example.com")), headers=headers_a
    ).json()

    r = api.client.get(f"/v1/notifications/{created['id']}", headers=headers_a)
    assert r.status_code == 200
    assert r.json()["status"] == "queued" and r.json()["attempts"] == 0

    assert api.client.get(f"/v1/notifications/{created['id']}", headers=headers_b).status_code == 404


def test_create_for_another_tenants_customer_is_rejected(api):
    _, headers_a = api.tenant_with_key("tenant-a")
    b = api.create_tenant("tenant-b")
    other_customer = api.add_customer(b["id"], "bob@example.com")

    r = api.client.post("/v1/notifications", json=_payload(other_customer), headers=headers_a)

    assert r.status_code == 422
    assert r.json()["detail"]["message"] == "Customer not found"
    assert api.notification_rows() == [] and api.queued_ids() == []


def test_create_requires_api_key(api):
    r = api.client.post("/v1/notifications", json=_payload(str(uuid.uuid4())))
    assert r.status_code == 401


def test_create_returns_503_and_saves_nothing_when_queue_is_down(api):
    tenant, headers = api.tenant_with_key("tenant-a")
    customer_id = api.add_customer(tenant["id"], "ada@example.com")
    # Auth already cached? No: a fresh key, so auth hits Postgres. Then Redis fails on enqueue.
    app.dependency_overrides[get_redis] = lambda: Redis.from_url(
        "redis://127.0.0.1:1/0", socket_connect_timeout=0.2
    )

    r = api.client.post("/v1/notifications", json=_payload(customer_id), headers=headers)

    assert r.status_code == 503
    assert api.notification_rows() == []


# --- idempotency -----------------------------------------------------------------


def test_same_idempotency_key_returns_original_instead_of_duplicate(api):
    tenant, headers = api.tenant_with_key("tenant-a")
    customer_id = api.add_customer(tenant["id"], "ada@example.com")
    headers = {**headers, "Idempotency-Key": "order-1234-shipped"}

    first = api.client.post("/v1/notifications", json=_payload(customer_id), headers=headers)
    second = api.client.post("/v1/notifications", json=_payload(customer_id), headers=headers)

    assert first.status_code == second.status_code == 202
    assert second.json()["id"] == first.json()["id"]
    assert second.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first.headers
    # Exactly one row and one job: no duplicate email.
    assert len(api.notification_rows()) == 1
    assert api.queued_ids() == [first.json()["id"]]


def test_idempotency_key_is_scoped_per_tenant(api):
    a, headers_a = api.tenant_with_key("tenant-a")
    b, headers_b = api.tenant_with_key("tenant-b")
    key = {"Idempotency-Key": "same-key"}

    ra = api.client.post(
        "/v1/notifications", json=_payload(api.add_customer(a["id"], "a@example.com")), headers={**headers_a, **key}
    )
    rb = api.client.post(
        "/v1/notifications", json=_payload(api.add_customer(b["id"], "b@example.com")), headers={**headers_b, **key}
    )

    assert ra.status_code == rb.status_code == 202
    assert ra.json()["id"] != rb.json()["id"]
    assert len(api.notification_rows()) == 2


def test_without_idempotency_key_each_request_creates_a_notification(api):
    tenant, headers = api.tenant_with_key("tenant-a")
    customer_id = api.add_customer(tenant["id"], "ada@example.com")

    ids = {api.client.post("/v1/notifications", json=_payload(customer_id), headers=headers).json()["id"] for _ in range(2)}

    assert len(ids) == 2
    assert len(api.queued_ids()) == 2


# --- batch -----------------------------------------------------------------------


def test_batch_returns_202_and_enqueues_all_in_order(api):
    tenant, headers = api.tenant_with_key("tenant-a")
    c1 = api.add_customer(tenant["id"], "a@example.com")
    c2 = api.add_customer(tenant["id"], "b@example.com")
    items = [_payload(c1, "one"), _payload(c2, "two"), _payload(c1, "three")]

    r = api.client.post("/v1/notifications/batch", json={"notifications": items}, headers=headers)

    assert r.status_code == 202
    returned = [i["id"] for i in r.json()["items"]]
    assert len(returned) == 3 and all(i["status"] == "queued" for i in r.json()["items"])
    assert api.queued_ids() == returned


def test_batch_over_100_is_rejected(api):
    tenant, headers = api.tenant_with_key("tenant-a")
    customer_id = api.add_customer(tenant["id"], "a@example.com")

    r = api.client.post(
        "/v1/notifications/batch", json={"notifications": [_payload(customer_id)] * 101}, headers=headers
    )

    assert r.status_code == 422
    assert api.notification_rows() == []


def test_batch_with_one_unknown_customer_creates_nothing(api):
    tenant, headers = api.tenant_with_key("tenant-a")
    good = api.add_customer(tenant["id"], "a@example.com")
    bad = str(uuid.uuid4())

    r = api.client.post(
        "/v1/notifications/batch", json={"notifications": [_payload(good), _payload(bad)]}, headers=headers
    )

    assert r.status_code == 422
    assert r.json()["detail"]["customer_ids"] == [bad]
    assert api.notification_rows() == [] and api.queued_ids() == []
