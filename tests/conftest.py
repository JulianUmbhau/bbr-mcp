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

    monkeypatch.setattr("bbr_mcp.graphql.asyncio.sleep", sleep)
    return delays


@pytest.fixture(autouse=True)
def mocked_http(request, monkeypatch):
    """Every client the server makes is answered by mock_http's routes (fresh per test); live tests use the network."""
    if request.node.get_closest_marker("live"):
        yield None
        return
    import httpx2
    import mock_http

    from bbr_mcp import http

    mock_http.ROUTER = router = mock_http.Router()
    monkeypatch.setattr(http, "TRANSPORT", httpx2.MockTransport(router.handle))
    yield router
