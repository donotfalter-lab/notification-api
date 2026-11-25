"""Seed demo tenants and customers.

Run inside the api container:
    docker compose exec api python scripts/seed.py

Safe to re-run: existing tenants and customers are skipped (ON CONFLICT DO NOTHING),
and Faker is seeded per tenant so the same emails are generated every time.
All emails use the reserved example.com domain, so nothing can reach a real inbox.
"""

import asyncio
import re
import time
import uuid

from faker import Faker
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.models import Customer, Tenant

TENANTS = [
    ("acme", "pro"),
    ("globex", "pro"),
    ("initech", "free"),
    ("umbrella", "free"),
    ("hooli", "free"),
]
CUSTOMERS_PER_TENANT = 2_000


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def build_customers(tenant_id: uuid.UUID, seed: int) -> list[dict]:
    fake = Faker()
    fake.seed_instance(seed)
    rows = []
    for n in range(CUSTOMERS_PER_TENANT):
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


async def seed() -> None:
    started = time.perf_counter()
    engine = create_async_engine(get_settings().database_url)

    async with engine.begin() as conn:
        tenant_rows = [{"id": uuid.uuid4(), "name": n, "plan": p} for n, p in TENANTS]
        result = await conn.execute(
            pg_insert(Tenant).values(tenant_rows).on_conflict_do_nothing(index_elements=["name"])
        )
        print(f"tenants: {result.rowcount} inserted, {len(TENANTS) - result.rowcount} already existed")

        names = [name for name, _ in TENANTS]
        tenant_ids = dict(
            (await conn.execute(select(Tenant.name, Tenant.id).where(Tenant.name.in_(names)))).all()
        )

        for i, (name, _) in enumerate(TENANTS):
            rows = build_customers(tenant_ids[name], seed=1_000 + i)
            result = await conn.execute(
                pg_insert(Customer)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["tenant_id", "email"])
            )
            print(f"  {name}: {result.rowcount} customers inserted")

    await engine.dispose()
    print(f"done in {time.perf_counter() - started:.1f}s")


if __name__ == "__main__":
    asyncio.run(seed())
