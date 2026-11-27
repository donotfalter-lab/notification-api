import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import enforce_rate_limit, get_current_tenant
from app.schemas import CustomerOut, CustomerPage
from app.services import customers as customer_service
from app.services.api_keys import CurrentTenant

router = APIRouter(prefix="/v1/customers", tags=["customers"], dependencies=[Depends(enforce_rate_limit)])


@router.get("", response_model=CustomerPage)
async def list_customers(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    tenant: CurrentTenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
):
    items, total = await customer_service.list_customers(
        session, tenant.id, limit=limit, offset=offset
    )
    return CustomerPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/{customer_id}", response_model=CustomerOut)
async def get_customer(
    customer_id: uuid.UUID,
    tenant: CurrentTenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
):
    customer = await customer_service.get_customer(session, tenant.id, customer_id)
    # 404 (not 403) for other tenants' customers, so we don't reveal they exist.
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return customer
