import pytest
import pytest_asyncio
import httpx
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from keyjack.database import Base, get_db
from keyjack.main import app
import os

# Use a separate test database
TEST_DATABASE_URL = "sqlite+aiosqlite:////tmp/test_keyjack_db.sqlite"
engine = create_async_engine(TEST_DATABASE_URL)
TestingSessionLocal = async_sessionmaker(autocommit=False, autoflush=False, bind=engine, class_=AsyncSession)

@pytest_asyncio.fixture
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    if os.path.exists("/tmp/test_keyjack_db.sqlite"):
        os.remove("/tmp/test_keyjack_db.sqlite")

async def override_get_db():
    async with TestingSessionLocal() as session:
        yield session

app.dependency_overrides[get_db] = override_get_db

@pytest.mark.asyncio
async def test_create_and_list_keys(setup_db):
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        # Create a key
        response = await ac.post("/api/keys", json={
            "provider": "openrouter",
            "key_value": "test-key-123",
            "daily_limit": 50
        })
        assert response.status_code == 200
        data = response.json()
        assert data["provider"] == "openrouter"
        assert "test****-123" in data["key_value"] or "****" in data["key_value"]

        # List keys
        response = await ac.get("/api/keys")
        assert response.status_code == 200
        assert len(response.json()) == 1

@pytest.mark.asyncio
async def test_proxy_unauthorized(setup_db):
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post("/v1/chat/completions",
            headers={"Authorization": "Bearer wrong"},
            json={"messages": []}
        )
        assert response.status_code == 401

@pytest.mark.asyncio
async def test_proxy_no_keys(setup_db):
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post("/v1/chat/completions",
            headers={"Authorization": "Bearer ali-super-secret-master-key"},
            json={"provider": "openrouter", "messages": []}
        )
        assert response.status_code == 503
        assert "No active keys available" in response.json()["detail"]
