import json
import logging

import httpx2
import mock_http as respx
import pytest

from bbr_mcp import graphql, http

URL = "https://graphql.datafordeler.dk/BBR/v3"
pytestmark = pytest.mark.anyio


def page(nodes, next_cursor=None):
    info = {"hasNextPage": next_cursor is not None, "endCursor": next_cursor}
    return httpx2.Response(200, json={"data": {"BBR_Bygning": {"nodes": nodes, "pageInfo": info}}})


async def run(limit=100):
    async with http.client() as client:
        return await graphql.query(client, "BBR/v3", "BBR_Bygning", {"husnummer": {"eq": "x"}}, "id_lokalId", limit)


@respx.mock
async def test_query_sends_key_time_and_filter_and_follows_pages(api_key):
    route = respx.post(URL).mock(side_effect=[page([{"id_lokalId": "a"}], "c1"), page([{"id_lokalId": "b"}])])
    rows, truncated = await run()
    assert rows == [{"id_lokalId": "a"}, {"id_lokalId": "b"}]
    assert truncated is False
    first, second = (json.loads(call.request.content) for call in route.calls)
    assert route.calls[0].request.url.params["apiKey"] == api_key
    assert first["variables"]["where"] == {"husnummer": {"eq": "x"}}
    assert first["variables"]["t"].endswith("Z") and len(first["variables"]["t"].split(".")[1]) == 7
    assert "registreringstid: $t, virkningstid: $t" in first["query"]
    assert second["variables"]["after"] == "c1"


@respx.mock
async def test_key_is_never_logged(api_key, caplog):
    respx.post(URL).mock(return_value=page([]))
    with caplog.at_level(logging.DEBUG):
        await run()
    assert api_key not in caplog.text


@respx.mock
async def test_query_stops_at_limit(api_key):
    respx.post(URL).mock(return_value=page([{"id_lokalId": "a"}, {"id_lokalId": "b"}], "c1"))
    rows, truncated = await run(limit=2)
    assert len(rows) == 2 and truncated is True


@respx.mock
async def test_retries_429_honouring_retry_after(api_key, no_sleep):
    respx.post(URL).mock(side_effect=[httpx2.Response(429, headers={"Retry-After": "3"}), page([])])
    assert await run() == ([], False)
    assert no_sleep == [3.0]


@respx.mock
async def test_gives_up_after_repeated_503(api_key, no_sleep):
    respx.post(URL).mock(return_value=httpx2.Response(503))
    with pytest.raises(graphql.DatafordelerError, match="503"):
        await run()
    assert no_sleep == [1.0, 2.0, 4.0]


@respx.mock
async def test_unauthorized_explains_the_key(api_key):
    respx.post(URL).mock(return_value=httpx2.Response(401, json={"title": "Unauthorized"}))
    with pytest.raises(graphql.DatafordelerError, match="15 minutes"):
        await run()


@respx.mock
async def test_graphql_errors_are_raised_without_the_key(api_key):
    respx.post(URL).mock(return_value=httpx2.Response(200, json={"errors": [{"message": f"bad {api_key}"}]}))
    with pytest.raises(graphql.DatafordelerError) as e:
        await run()
    assert api_key not in str(e.value) and "bad ***" in str(e.value)


async def test_missing_key(monkeypatch):
    monkeypatch.delenv("DATAFORDELER_API_KEY", raising=False)
    with pytest.raises(graphql.DatafordelerError, match="DATAFORDELER_API_KEY"):
        await run()
