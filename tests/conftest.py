import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("DATAFORDELER_API_KEY", "test-key-123")
    return "test-key-123"


@pytest.fixture
def no_sleep(monkeypatch):
    delays = []

    async def sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr("datafordeler_mcp.graphql.asyncio.sleep", sleep)
    return delays
