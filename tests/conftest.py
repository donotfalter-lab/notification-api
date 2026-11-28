import asyncio
import os
import uuid
from dataclasses import dataclass

# Settings are required env vars; give tests harmless values before the app imports.
# Inside docker compose the real values from .env are used instead.
os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("POSTGRES_DB", "test")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token-0123456789")
os.environ.setdefault("SMTP_HOST", "localhost")
os.environ.setdefault("SMTP_FROM", "notifications@example.com")

import fakeredis  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.cache import get_redis  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, Customer  # noqa: E402


@pytest.fixture(autouse=True)
def _loadtest_mode_off(monkeypatch):
    """Tests assume normal quotas, even if .env has LOADTEST_MODE=true for a load test."""
    monkeypatch.setattr(get_settings(), "loadtest_mode", False)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# --- Postgres-backed tests -------------------------------------------------
# These use a separate "<POSTGRES_DB>_test" database on the same server, created
# on first run and rebuilt from the models each session. Skipped if Postgres
# isn't reachable (e.g. running pytest outside docker compose).


@pytest.fixture(scope="session")
def test_engine() -> AsyncEngine:
    settings = get_settings()
    test_url = settings.database_url.set(database=f"{settings.postgres_db}_test")

    async def prepare() -> AsyncEngine:
        server = create_async_engine(
            settings.database_url.set(database="postgres"),
            isolation_level="AUTOCOMMIT",
            poolclass=NullPool,
        )
        try:
            async with server.connect() as conn:
                exists = await conn.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"),
                    {"name": test_url.database},
                )
                if not exists:
                    await conn.execute(text(f'CREATE DATABASE "{test_url.database}"'))
        finally:
            await server.dispose()

        engine = create_async_engine(test_url, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        return engine

    try:
        return asyncio.run(asyncio.wait_for(prepare(), timeout=10))
    except (OSError, SQLAlchemyError, TimeoutError) as exc:
        pytest.skip(f"Postgres not reachable for DB tests: {exc}")


@dataclass
class Api:
    """Test helper: an authenticated-ready client plus shortcuts for setup."""

    client: TestClient
    engine: AsyncEngine
    redis_server: fakeredis.FakeServer

    @property
    def admin(self) -> dict:
        return {"Authorization": f"Bearer {get_settings().admin_token.get_secret_value()}"}

    def create_tenant(self, name: str, plan: str = "free") -> dict:
        r = self.client.post("/admin/tenants", json={"name": name, "plan": plan}, headers=self.admin)
        assert r.status_code == 201, r.text
        return r.json()

    def create_key(self, tenant_id: str) -> dict:
        r = self.client.post(f"/admin/tenants/{tenant_id}/api-keys", headers=self.admin)
        assert r.status_code == 201, r.text
        return r.json()

    def add_customer(self, tenant_id: str, email: str, name: str = "Test Customer") -> str:
        customer_id = uuid.uuid4()

        async def insert():
            async with self.engine.begin() as conn:
                await conn.execute(
                    Customer.__table__.insert().values(
                        id=customer_id, tenant_id=uuid.UUID(tenant_id), email=email, name=name
                    )
                )

        asyncio.run(insert())
        return str(customer_id)

    def redis(self) -> fakeredis.FakeAsyncRedis:
        return fakeredis.FakeAsyncRedis(server=self.redis_server, decode_responses=True)

    def run(self, fn):
        """Run `async fn(redis)` against the shared fake Redis and return its result."""

        async def go():
            return await fn(self.redis())

        return asyncio.run(go())

    def queued_ids(self) -> list[str]:
        """Ids waiting in the Redis queue, oldest first."""
        return list(reversed(self.run(lambda r: r.lrange("notifications:queue", 0, -1))))

    def notification_rows(self) -> list[dict]:
        async def rows():
            async with self.engine.connect() as conn:
                result = await conn.execute(text("SELECT * FROM notifications ORDER BY created_at"))
                return [dict(r._mapping) for r in result]

        return asyncio.run(rows())

    def tenant_with_key(self, name: str) -> tuple[dict, dict]:
        """Create a tenant and return (tenant, X-API-Key headers)."""
        tenant = self.create_tenant(name)
        return tenant, {"X-API-Key": self.create_key(tenant["id"])["api_key"]}


@pytest.fixture
def api(client, test_engine) -> Api:
    async def truncate():
        async with test_engine.begin() as conn:
            await conn.execute(text("TRUNCATE tenants, api_keys, customers, notifications CASCADE"))

    asyncio.run(truncate())

    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    redis_server = fakeredis.FakeServer()

    async def _get_session():
        async with sessions() as session:
            yield session

    def _get_redis():
        # New client per request: each request runs on the TestClient's event loop.
        return fakeredis.FakeAsyncRedis(server=redis_server, decode_responses=True)

    app.dependency_overrides[get_session] = _get_session
    app.dependency_overrides[get_redis] = _get_redis
    return Api(client=client, engine=test_engine, redis_server=redis_server)
