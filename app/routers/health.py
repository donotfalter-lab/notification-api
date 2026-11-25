import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import HealthOut

router = APIRouter(tags=["health"])

DB_CHECK_TIMEOUT_SECONDS = 2


@router.get("/health", response_model=HealthOut, responses={503: {"model": HealthOut}})
async def health(session: AsyncSession = Depends(get_session)):
    try:
        await asyncio.wait_for(
            session.execute(text("SELECT 1")), timeout=DB_CHECK_TIMEOUT_SECONDS
        )
    except Exception:
        return JSONResponse(
            status_code=503, content={"status": "error", "database": "unreachable"}
        )
    return HealthOut(status="ok", database="ok")
