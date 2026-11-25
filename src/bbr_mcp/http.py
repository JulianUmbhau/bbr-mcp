"""The HTTP client for Datafordeler and Adressevælger: httpx2, the client the MCP SDK itself uses."""

import httpx2

# A transport for every client, when set: the tests answer the requests with a mock.
TRANSPORT: httpx2.AsyncBaseTransport | None = None


def client() -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=TRANSPORT) if TRANSPORT else httpx2.AsyncClient()
