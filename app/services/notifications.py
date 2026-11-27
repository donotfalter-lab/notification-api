"""Creating and reading notifications. Sending happens in worker/, never here."""

import logging
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Customer, Notification
from app.schemas import NotificationCreate
from app.services import queue

logger = logging.getLogger(__name__)


class UnknownCustomers(Exception):
    """One or more customer_ids don't exist for this tenant."""

    def __init__(self, customer_ids: list[uuid.UUID]):
        self.customer_ids = customer_ids
        super().__init__(f"Unknown customer_id(s): {customer_ids}")


class QueueUnavailable(Exception):
    """Redis was unreachable, so the notification was not accepted."""


@dataclass
class Accepted:
    id: uuid.UUID
    status: str
    replayed: bool = False  # True when an Idempotency-Key matched an earlier request


async def _check_customers(session: AsyncSession, tenant_id: uuid.UUID, ids: set[uuid.UUID]) -> None:
    found = set(
        await session.scalars(
            select(Customer.id).where(Customer.tenant_id == tenant_id, Customer.id.in_(ids))
        )
    )
    missing = ids - found
    if missing:
        raise UnknownCustomers(sorted(missing, key=str))


async def _enqueue_or_rollback(
    session: AsyncSession, redis: Redis, ids: list[uuid.UUID]
) -> None:
    """Push to Redis; if that fails, delete the rows so a client retry starts clean."""
    try:
        await queue.enqueue(redis, *ids)
    except RedisError as exc:
        logger.error("Could not enqueue %d notification(s): %s", len(ids), exc)
        await session.execute(delete(Notification).where(Notification.id.in_(ids)))
        await session.commit()
        raise QueueUnavailable from exc


async def create_notification(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    data: NotificationCreate,
    idempotency_key: str | None = None,
) -> Accepted:
    await _check_customers(session, tenant_id, {data.customer_id})

    new_id = await session.scalar(
        pg_insert(Notification)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            customer_id=data.customer_id,
            subject=data.subject,
            body=data.body,
            status="queued",
            idempotency_key=idempotency_key,
        )
        # Same tenant + same key: insert nothing (also safe if two requests race).
        .on_conflict_do_nothing(
            index_elements=["tenant_id", "idempotency_key"],
            index_where=Notification.idempotency_key.is_not(None),
        )
        .returning(Notification.id)
    )

    if new_id is None:
        await session.rollback()
        original = await session.scalar(
            select(Notification).where(
                Notification.tenant_id == tenant_id,
                Notification.idempotency_key == idempotency_key,
            )
        )
        return Accepted(id=original.id, status=original.status, replayed=True)

    await session.commit()
    await _enqueue_or_rollback(session, redis, [new_id])
    return Accepted(id=new_id, status="queued")


async def create_batch(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    items: list[NotificationCreate],
) -> list[Accepted]:
    """All-or-nothing: if any customer_id is unknown, nothing is created."""
    await _check_customers(session, tenant_id, {item.customer_id for item in items})

    rows = [
        {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "customer_id": item.customer_id,
            "subject": item.subject,
            "body": item.body,
            "status": "queued",
        }
        for item in items
    ]
    await session.execute(insert(Notification), rows)
    await session.commit()

    ids = [row["id"] for row in rows]
    await _enqueue_or_rollback(session, redis, ids)
    return [Accepted(id=i, status="queued") for i in ids]


async def get_notification(
    session: AsyncSession, tenant_id: uuid.UUID, notification_id: uuid.UUID
) -> Notification | None:
    return await session.scalar(
        select(Notification).where(
            Notification.id == notification_id, Notification.tenant_id == tenant_id
        )
    )
