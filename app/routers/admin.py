import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import get_redis
from app.db import get_session
from app.deps import require_admin
from app.models import Tenant
from app.schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    DeliveryStats,
    QueueStats,
    ReplayResult,
    TenantCreate,
    TenantOut,
)
from app.services import api_keys as api_key_service
from app.services import queue
from app.services import stats as stats_service

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.post("/tenants", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def create_tenant(body: TenantCreate, session: AsyncSession = Depends(get_session)):
    tenant = Tenant(name=body.name, plan=body.plan)
    session.add(tenant)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="A tenant with that name already exists")
    await session.refresh(tenant)
    return tenant


@router.post(
    "/tenants/{tenant_id}/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_api_key(
    tenant_id: uuid.UUID,
    body: ApiKeyCreate | None = None,
    session: AsyncSession = Depends(get_session),
):
    if await session.get(Tenant, tenant_id) is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    name = body.name if body else ApiKeyCreate().name
    api_key, raw_key = await api_key_service.create_api_key(session, tenant_id, name)
    return ApiKeyCreated(**ApiKeyOut.model_validate(api_key).model_dump(), api_key=raw_key)


@router.post("/api-keys/{api_key_id}/revoke", response_model=ApiKeyOut)
async def revoke_api_key(
    api_key_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    api_key = await api_key_service.revoke_api_key(session, redis, api_key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    return api_key


@router.get("/queue/stats", response_model=QueueStats)
async def queue_stats(redis: Redis = Depends(get_redis)):
    return await queue.stats(redis)


@router.post("/dead-letter/replay", response_model=ReplayResult)
async def replay_dead_letter(
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    return ReplayResult(replayed=await queue.replay_dead_letter(session, redis))


@router.get("/stats/delivery", response_model=DeliveryStats)
async def delivery_stats(
    minutes: int = Query(5, ge=1, le=1440, description="Look at notifications created in the last N minutes"),
    since: datetime | None = Query(
        None, description="Or: only notifications created at/after this time (ISO 8601). Overrides minutes."
    ),
    session: AsyncSession = Depends(get_session),
):
    """Per tenant: how many notifications, and p50/p95/p99 of sent_at - created_at."""
    tenants = await stats_service.delivery_stats(session, minutes, since)
    return DeliveryStats(window_minutes=minutes, since=since, tenants=tenants)
