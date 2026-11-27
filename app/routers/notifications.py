import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import get_redis
from app.db import get_session
from app.deps import get_current_tenant
from app.schemas import (
    NotificationAccepted,
    NotificationBatchAccepted,
    NotificationBatchCreate,
    NotificationCreate,
    NotificationOut,
)
from app.services import notifications as notification_service
from app.services.api_keys import CurrentTenant
from app.services.notifications import QueueUnavailable, UnknownCustomers

router = APIRouter(prefix="/v1/notifications", tags=["notifications"])


def _unknown_customers(exc: UnknownCustomers) -> HTTPException:
    # Same answer whether the customer doesn't exist or belongs to another tenant.
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"message": "Customer not found", "customer_ids": [str(i) for i in exc.customer_ids]},
    )


def _queue_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Queue unavailable, notification not accepted. Retry later.",
    )


@router.post("", response_model=NotificationAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_notification(
    body: NotificationCreate,
    response: Response,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        description="Repeat a request with the same key to get the original notification back.",
    ),
    tenant: CurrentTenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    try:
        accepted = await notification_service.create_notification(
            session, redis, tenant.id, body, idempotency_key
        )
    except UnknownCustomers as exc:
        raise _unknown_customers(exc)
    except QueueUnavailable:
        raise _queue_unavailable()

    if accepted.replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return NotificationAccepted(id=accepted.id, status=accepted.status)


@router.post("/batch", response_model=NotificationBatchAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_notification_batch(
    body: NotificationBatchCreate,
    tenant: CurrentTenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    try:
        accepted = await notification_service.create_batch(session, redis, tenant.id, body.notifications)
    except UnknownCustomers as exc:
        raise _unknown_customers(exc)
    except QueueUnavailable:
        raise _queue_unavailable()
    return NotificationBatchAccepted(
        items=[NotificationAccepted(id=a.id, status=a.status) for a in accepted]
    )


@router.get("/{notification_id}", response_model=NotificationOut)
async def get_notification(
    notification_id: uuid.UUID,
    tenant: CurrentTenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
):
    notification = await notification_service.get_notification(session, tenant.id, notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    return notification
