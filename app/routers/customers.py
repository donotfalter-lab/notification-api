import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import CustomerOut, CustomerPage
from app.services import customers as customer_service

router = APIRouter(prefix="/v1/customers", tags=["customers"])


# NOTE: no auth yet. Once API keys are wired up, tenant_id will come from the
# key instead of a query parameter, and every query will be scoped to it.


@router.get("", response_model=CustomerPage)
async def list_customers(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    tenant_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
):
    items, total = await customer_service.list_customers(
        session, limit=limit, offset=offset, tenant_id=tenant_id
    )
    return CustomerPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/{customer_id}", response_model=CustomerOut)
async def get_customer(
    customer_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    customer = await customer_service.get_customer(session, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return customer
