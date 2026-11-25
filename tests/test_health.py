from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.main import app


def _override_session_with(url: str):
    engine = create_async_engine(url, poolclass=NullPool)
    sessions = async_sessionmaker(engine)

    async def _get_session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = _get_session


def test_health_ok_when_database_reachable(client):
    # An in-memory SQLite database stands in for a reachable database.
    _override_session_with("sqlite+aiosqlite:///:memory:")

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_health_503_when_database_unreachable(client):
    # Nothing listens on port 1, so the connection is refused immediately.
    _override_session_with("postgresql+asyncpg://u:p@127.0.0.1:1/none")

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "unreachable"}
