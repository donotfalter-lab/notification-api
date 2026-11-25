import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


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
