import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import get_redis
from app.config import get_settings
from app.db import get_session
from app.deps import enforce_rate_limit, get_current_tenant
from app.schemas import (
    NotificationAccepted,
    NotificationBatchAccepted,
    NotificationBatchCreate,
    NotificationCreate,
    NotificationOut,
)
from app.services import notifications as notification_service
from app.services.api_keys import CurrentTenant
from app.services.limits import reserve_email_quota
from app.services.notifications import QueueUnavailable, UnknownCustomers

router = APIRouter(
    prefix="/v1/notifications", tags=["notifications"], dependencies=[Depends(enforce_rate_limit)]
)


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
    # A repeat of an earlier request sends nothing new: answer it before touching the quota.
    if idempotency_key:
        original = await notification_service.find_by_idempotency_key(session, tenant.id, idempotency_key)
        if original is not None:
            response.headers["Idempotent-Replayed"] = "true"
            return NotificationAccepted(id=original.id, status=original.status)

    quota = await reserve_email_quota(redis, tenant.id, get_settings().plan_limits(tenant.plan), 1)
    try:
        accepted = await notification_service.create_notification(
            session, redis, tenant.id, body, idempotency_key
        )
    except UnknownCustomers as exc:
        await quota.refund()
        raise _unknown_customers(exc)
    except QueueUnavailable:
        await quota.refund()
        raise _queue_unavailable()
    except BaseException:
        await quota.refund()
        raise

    if accepted.replayed:  # lost a race with an identical request; that one was counted
        await quota.refund()
        response.headers["Idempotent-Replayed"] = "true"
    return NotificationAccepted(id=accepted.id, status=accepted.status)


@router.post("/batch", response_model=NotificationBatchAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_notification_batch(
    body: NotificationBatchCreate,
    tenant: CurrentTenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    # Every email in the batch counts; the whole batch is rejected if it doesn't fit.
    quota = await reserve_email_quota(
        redis, tenant.id, get_settings().plan_limits(tenant.plan), len(body.notifications)
    )
    try:
        accepted = await notification_service.create_batch(session, redis, tenant.id, body.notifications)
    except UnknownCustomers as exc:
        await quota.refund()
        raise _unknown_customers(exc)
    except QueueUnavailable:
        await quota.refund()
        raise _queue_unavailable()
    except BaseException:
        await quota.refund()
        raise
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
