"""Client for the Datafordeler GraphQL services (https://graphql.datafordeler.dk/<register>/<version>)."""

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any

import httpx

BASE_URL = os.environ.get("DATAFORDELER_GRAPHQL_URL", "https://graphql.datafordeler.dk")
TIMEOUT = 30.0
MAX_ATTEMPTS = 4
RETRY_STATUSES = {429, 502, 503, 504}
PAGE_SIZE = 100

# Datafordeler takes the key only as the apiKey URL parameter, and httpx logs every request URL at INFO: keep it quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)


class DatafordelerError(Exception):
    pass


def _api_key() -> str:
    key = os.environ.get("DATAFORDELER_API_KEY", "").strip()
    if not key:
        raise DatafordelerError(
            "DATAFORDELER_API_KEY is not set: create an IT-system with an API key at https://portal.datafordeler.dk"
        )
    return key


def now() -> str:
    """The current time as DafDateTime (RFC 3339 with six fractional digits)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        try:
            return min(float(response.headers.get("Retry-After", "")), 30.0)
        except ValueError:
            pass
    return float(2**attempt)


def _clean(message: str, key: str) -> str:
    return message.replace(key, "***")


async def _post(client: httpx.AsyncClient, path: str, body: dict[str, Any]) -> dict[str, Any]:
    key = _api_key()
    for attempt in range(MAX_ATTEMPTS):
        response = None
        try:
            response = await client.post(f"{BASE_URL}/{path}", params={"apiKey": key}, json=body, timeout=TIMEOUT)
        except httpx.TransportError as e:
            if attempt == MAX_ATTEMPTS - 1:
                raise DatafordelerError(_clean(f"Datafordeler unreachable: {e!r}", key)) from None
        else:
            if response.status_code not in RETRY_STATUSES:
                break
            if attempt == MAX_ATTEMPTS - 1:
                raise DatafordelerError(f"Datafordeler answered {response.status_code} after {MAX_ATTEMPTS} attempts")
        await asyncio.sleep(_retry_delay(response, attempt))

    assert response is not None
    if response.status_code in (401, 403):
        raise DatafordelerError(
            f"Datafordeler refused the API key ({response.status_code}): check DATAFORDELER_API_KEY; "
            "a new key takes about 15 minutes to become active"
        )
    try:
        payload = response.json()
    except ValueError:
        raise DatafordelerError(f"Datafordeler answered {response.status_code} with no JSON") from None
    if payload.get("errors"):
        messages = "; ".join(str(e.get("message", e)) for e in payload["errors"])
        raise DatafordelerError(_clean(f"Datafordeler query failed: {messages}", key))
    if response.status_code != 200:
        raise DatafordelerError(f"Datafordeler answered {response.status_code}")
    return payload.get("data") or {}


async def query(
    client: httpx.AsyncClient,
    register: str,
    entity: str,
    where: dict[str, Any],
    fields: str,
    limit: int = PAGE_SIZE,
) -> tuple[list[dict[str, Any]], bool]:
    """The current rows of `entity` matching `where`, at most `limit`, and whether more were left out."""
    document = (
        f"query($first: Int, $after: String, $t: DafDateTime, $where: {entity}FilterInput) {{ "
        f"{entity}(first: $first, after: $after, registreringstid: $t, virkningstid: $t, where: $where) {{ "
        f"nodes {{ {fields} }} pageInfo {{ hasNextPage endCursor }} }} }}"
    )
    t = now()
    rows: list[dict[str, Any]] = []
    after = None
    while True:
        variables = {"first": min(PAGE_SIZE, limit - len(rows)), "after": after, "t": t, "where": where}
        data = await _post(client, register, {"query": document, "variables": variables})
        page = data.get(entity) or {}
        rows.extend(page.get("nodes") or [])
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return rows, False
        if len(rows) >= limit:
            return rows, True
        after = info.get("endCursor")
