import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Customer


async def get_customer(session: AsyncSession, customer_id: uuid.UUID) -> Customer | None:
    return await session.get(Customer, customer_id)


async def list_customers(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    tenant_id: uuid.UUID | None = None,
) -> tuple[list[Customer], int]:
    """Return one page of customers plus the total count."""
    query = select(Customer)
    count_query = select(func.count()).select_from(Customer)
    if tenant_id is not None:
        query = query.where(Customer.tenant_id == tenant_id)
        count_query = count_query.where(Customer.tenant_id == tenant_id)

    # Stable ordering so pages don't shuffle between requests.
    query = query.order_by(Customer.created_at, Customer.id).limit(limit).offset(offset)

    items = (await session.scalars(query)).all()
    total = await session.scalar(count_query)
    return list(items), total or 0
