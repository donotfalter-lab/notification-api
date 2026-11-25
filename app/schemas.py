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
