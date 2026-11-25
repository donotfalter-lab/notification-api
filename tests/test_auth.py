import asyncio
import re

from sqlalchemy import text

from app.deps import INVALID_API_KEY
from app.services.api_keys import CACHE_KEY_PREFIX, hash_api_key


def _key(raw: str) -> dict:
    return {"X-API-Key": raw}


# --- API key auth ------------------------------------------------------------


def test_missing_key_returns_401(api):
    r = api.client.get("/v1/customers")
    assert r.status_code == 401
    assert r.json() == {"detail": INVALID_API_KEY}


def test_wrong_key_returns_401_with_same_message(api):
    # Well-formed but unknown key, and plain garbage: both look like a missing key.
    for bad in ["ntf_live_" + "x" * 32, "nope"]:
        r = api.client.get("/v1/customers", headers=_key(bad))
        assert r.status_code == 401
        assert r.json() == {"detail": INVALID_API_KEY}


def test_valid_key_lists_only_own_tenants_customers(api):
    a = api.create_tenant("tenant-a")
    b = api.create_tenant("tenant-b")
    api.add_customer(a["id"], "a1@example.com")
    api.add_customer(b["id"], "b1@example.com")
    key_a = api.create_key(a["id"])["api_key"]

    r = api.client.get("/v1/customers", headers=_key(key_a))

    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert [c["email"] for c in body["items"]] == ["a1@example.com"]


def test_tenant_cannot_read_another_tenants_customer(api):
    a = api.create_tenant("tenant-a")
    b = api.create_tenant("tenant-b")
    own_id = api.add_customer(a["id"], "a1@example.com")
    other_id = api.add_customer(b["id"], "b1@example.com")
    key_a = api.create_key(a["id"])["api_key"]

    assert api.client.get(f"/v1/customers/{own_id}", headers=_key(key_a)).status_code == 200

    r = api.client.get(f"/v1/customers/{other_id}", headers=_key(key_a))
    # 404, not 403: tenant A can't even learn that the customer exists.
    assert r.status_code == 404
    assert r.json() == {"detail": "Customer not found"}


def test_revoked_key_stops_working_immediately(api):
    tenant = api.create_tenant("tenant-a")
    created = api.create_key(tenant["id"])
    headers = _key(created["api_key"])

    # First request succeeds and puts the key in the Redis cache.
    assert api.client.get("/v1/customers", headers=headers).status_code == 200

    r = api.client.post(f"/admin/api-keys/{created['id']}/revoke", headers=api.admin)
    assert r.status_code == 200
    assert r.json()["revoked_at"] is not None

    # No waiting for the 60s cache TTL: revocation clears the cache entry.
    r = api.client.get("/v1/customers", headers=headers)
    assert r.status_code == 401
    assert r.json() == {"detail": INVALID_API_KEY}


def test_valid_key_lookup_is_cached_in_redis(api):
    tenant = api.create_tenant("tenant-a")
    raw = api.create_key(tenant["id"])["api_key"]
    cache_key = CACHE_KEY_PREFIX + hash_api_key(raw)

    assert api.client.get("/v1/customers", headers=_key(raw)).status_code == 200

    async def ttl():
        return await api.redis().ttl(cache_key)

    assert 0 < asyncio.run(ttl()) <= 60


# --- admin endpoints -----------------------------------------------------------


def test_admin_endpoints_require_admin_token(api):
    assert api.client.post("/admin/tenants", json={"name": "x"}).status_code == 401
    r = api.client.post(
        "/admin/tenants", json={"name": "x"}, headers={"Authorization": "Bearer wrong-token"}
    )
    assert r.status_code == 401


def test_create_api_key_returns_raw_key_once_and_stores_only_hash(api):
    tenant = api.create_tenant("tenant-a", plan="pro")
    created = api.create_key(tenant["id"])

    raw = created["api_key"]
    assert re.fullmatch(r"ntf_live_[A-Za-z0-9_-]{32}", raw)
    assert created["key_prefix"] == raw[:12]

    # The raw key is not stored anywhere in the database.
    async def stored_columns():
        async with api.engine.connect() as conn:
            return (await conn.execute(text("SELECT key_prefix, key_hash FROM api_keys"))).one()

    prefix, key_hash = asyncio.run(stored_columns())
    assert prefix == raw[:12]
    assert key_hash == hash_api_key(raw) != raw


def test_duplicate_tenant_name_returns_409(api):
    api.create_tenant("tenant-a")
    r = api.client.post("/admin/tenants", json={"name": "tenant-a"}, headers=api.admin)
    assert r.status_code == 409
