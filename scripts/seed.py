"""Seed demo tenants, customers, and one API key per tenant.

Run inside the api container:
    docker compose exec api python scripts/seed.py

Safe to re-run:
- existing tenants and customers are skipped (ON CONFLICT DO NOTHING), and Faker
  is seeded per tenant so the same emails are generated every time;
- API keys already in loadtests/keys.json are reused if they're still valid,
  otherwise a new key is created for that tenant.

All emails use the reserved example.com domain, so nothing can reach a real inbox.
Raw API keys and 10 customer ids per tenant are written to loadtests/keys.json
(gitignored) for the k6 load tests.
"""

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path

from faker import Faker
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.config import get_settings
from app.models import ApiKey, Customer, Tenant
from app.services.api_keys import create_api_key, hash_api_key

TENANTS = [
    ("acme", "pro"),
    ("globex", "pro"),
    ("initech", "free"),
    ("umbrella", "free"),
    ("hooli", "free"),
]
CUSTOMERS_PER_TENANT = 2_000
SEED_KEY_NAME = "seed"
CUSTOMER_IDS_PER_TENANT = 10  # written to keys.json for the k6 load tests
KEYS_FILE = Path(__file__).resolve().parent.parent / "loadtests" / "keys.json"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def build_customers(tenant_id: uuid.UUID, seed: int, count: int = CUSTOMERS_PER_TENANT) -> list[dict]:
    fake = Faker()
    fake.seed_instance(seed)
    rows = []
    for n in range(count):
        first, last = fake.first_name(), fake.last_name()
        rows.append(
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                # The index keeps emails unique within a tenant even if names repeat.
                "email": f"{_slug(first)}.{_slug(last)}.{n:04d}@example.com",
                "name": f"{first} {last}",
            }
        )
    return rows


def load_existing_keys(keys_file: Path) -> dict[str, str]:
    """Return {tenant_name: raw_key} from a previous run, if any."""
    try:
        entries = json.loads(keys_file.read_text())
        return {e["tenant"]: e["api_key"] for e in entries}
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
        return {}


async def key_is_active(session: AsyncSession, tenant_id: uuid.UUID, raw_key: str) -> bool:
    found = await session.scalar(
        select(ApiKey.id).where(
            ApiKey.tenant_id == tenant_id,
            ApiKey.key_hash == hash_api_key(raw_key),
            ApiKey.revoked_at.is_(None),
        )
    )
    return found is not None


async def sample_customer_ids(session: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    """A stable handful of this tenant's customers for load tests to send to."""
    ids = await session.scalars(
        select(Customer.id)
        .where(Customer.tenant_id == tenant_id)
        .order_by(Customer.email)
        .limit(CUSTOMER_IDS_PER_TENANT)
    )
    return [str(i) for i in ids]


def write_keys_file(entries: list[dict], keys_file: Path) -> None:
    keys_file.parent.mkdir(parents=True, exist_ok=True)
    keys_file.write_text(json.dumps(entries, indent=2) + "\n")
    os.chmod(keys_file, 0o600)  # owner-only: these are credentials
    # In docker the seed runs as root on the bind-mounted project folder; hand the
    # file (and loadtests/, if we just created it) to whoever owns the project
    # (you, on the host) so you can actually read it.
    if os.geteuid() == 0:
        project = keys_file.parent.parent.stat()
        for path in (keys_file.parent, keys_file):
            os.chown(path, project.st_uid, project.st_gid)


async def seed(
    engine: AsyncEngine | None = None,
    keys_file: Path = KEYS_FILE,
    customers_per_tenant: int = CUSTOMERS_PER_TENANT,
) -> list[dict]:
    """Seed the database and write keys_file. Returns the keys.json entries."""
    started = time.perf_counter()
    own_engine = engine is None
    engine = engine or create_async_engine(get_settings().database_url)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        # Tenants
        tenant_rows = [{"id": uuid.uuid4(), "name": n, "plan": p} for n, p in TENANTS]
        result = await session.execute(
            pg_insert(Tenant).values(tenant_rows).on_conflict_do_nothing(index_elements=["name"])
        )
        print(f"tenants: {result.rowcount} inserted, {len(TENANTS) - result.rowcount} already existed")

        names = [name for name, _ in TENANTS]
        tenant_ids = dict(
            (await session.execute(select(Tenant.name, Tenant.id).where(Tenant.name.in_(names)))).all()
        )

        # Customers
        for i, (name, _) in enumerate(TENANTS):
            rows = build_customers(tenant_ids[name], seed=1_000 + i, count=customers_per_tenant)
            result = await session.execute(
                pg_insert(Customer)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["tenant_id", "email"])
            )
            print(f"  {name}: {result.rowcount} customers inserted")
        await session.commit()

        # API keys: one per tenant, reused across runs while still valid.
        existing = load_existing_keys(keys_file)
        entries = []
        print(f"\nAPI keys (also written to {keys_file.name}, with {CUSTOMER_IDS_PER_TENANT} customer ids each):")
        for name, plan in TENANTS:
            tenant_id = tenant_ids[name]
            raw_key = existing.get(name)
            if raw_key and await key_is_active(session, tenant_id, raw_key):
                status = "reused"
            else:
                _, raw_key = await create_api_key(session, tenant_id, SEED_KEY_NAME)
                status = "created"
            entries.append(
                {
                    "tenant": name,
                    "plan": plan,
                    "tenant_id": str(tenant_id),
                    "api_key": raw_key,
                    "customer_ids": await sample_customer_ids(session, tenant_id),
                }
            )
            print(f"  {name:<9} {plan:<4} {raw_key}  ({status})")

    write_keys_file(entries, keys_file)
    if own_engine:
        await engine.dispose()
    print(f"\ndone in {time.perf_counter() - started:.1f}s")
    return entries


if __name__ == "__main__":
    asyncio.run(seed())
