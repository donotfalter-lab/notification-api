import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    name: str
    created_at: datetime


class CustomerPage(BaseModel):
    items: list[CustomerOut]
    total: int
    limit: int
    offset: int


class HealthOut(BaseModel):
    status: str
    database: str


# --- admin ---


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    plan: Literal["free", "pro"] = "free"


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    plan: str
    created_at: datetime


class ApiKeyCreate(BaseModel):
    name: str = Field(default="default", min_length=1, max_length=100)


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    key_prefix: str
    created_at: datetime
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    api_key: str = Field(description="The raw key. Shown only once; store it now.")


# --- notifications ---


class NotificationCreate(BaseModel):
    customer_id: uuid.UUID
    subject: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1, max_length=50_000)


class NotificationAccepted(BaseModel):
    id: uuid.UUID
    status: str


class NotificationBatchCreate(BaseModel):
    notifications: list[NotificationCreate] = Field(min_length=1, max_length=100)


class NotificationBatchAccepted(BaseModel):
    items: list[NotificationAccepted]


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    customer_id: uuid.UUID
    channel: str
    subject: str
    body: str
    status: str
    attempts: int
    last_error: str | None
    created_at: datetime
    sent_at: datetime | None


# --- admin: queue ---


class QueueStats(BaseModel):
    queued: int
    processing: int
    delayed: int
    dead_letter: int
    workers: int


class ReplayResult(BaseModel):
    replayed: int


# --- admin: delivery stats ---


class LatencyMs(BaseModel):
    p50: float | None
    p95: float | None
    p99: float | None
    max: float | None


class TenantDeliveryStats(BaseModel):
    tenant_id: uuid.UUID
    tenant: str
    plan: str
    created: int = Field(description="Notifications created in the window")
    sent: int
    pending: int = Field(description="Still queued (includes ones waiting to retry)")
    failed: int
    latency_ms: LatencyMs = Field(description="sent_at - created_at, over sent notifications only")


class DeliveryStats(BaseModel):
    window_minutes: int
    since: datetime | None = Field(default=None, description="Set when the window was given as a start time")
    tenants: list[TenantDeliveryStats]
