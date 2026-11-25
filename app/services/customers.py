import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Customer


async def get_customer(
    session: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID
) -> Customer | None:
    """Return the customer only if it belongs to this tenant."""
    return await session.scalar(
        select(Customer).where(Customer.id == customer_id, Customer.tenant_id == tenant_id)
    )


async def list_customers(
    session: AsyncSession, tenant_id: uuid.UUID, *, limit: int, offset: int
) -> tuple[list[Customer], int]:
    """Return one page of this tenant's customers plus their total count."""
    query = (
        select(Customer)
        .where(Customer.tenant_id == tenant_id)
        # Stable ordering so pages don't shuffle between requests.
        .order_by(Customer.created_at, Customer.id)
        .limit(limit)
        .offset(offset)
    )
    count_query = select(func.count()).select_from(Customer).where(Customer.tenant_id == tenant_id)

    items = (await session.scalars(query)).all()
    total = await session.scalar(count_query)
    return list(items), total or 0
