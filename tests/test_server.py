import json

import httpx
import pytest
import respx
from mcp import Client

from datafordeler_mcp.server import mcp

pytestmark = pytest.mark.anyio

ADDRESS_ID = "0a3f509e-fb66-32b8-e044-0003ba298018"
HOUSE_ID = "0a3f507a-9d13-32b8-e044-0003ba298018"
BBR_URL = "https://graphql.datafordeler.dk/BBR/v3"
HOUSE = {
    "id_lokalid": HOUSE_ID,
    "husnummertekst": "120",
    "adgangsadressebetegnelse": "Jagtvej 120, 2200 København N",
    "vejnavn": "Jagtvej",
    "status": "3",
    "adgangspunkt": {"koordinater": {"x": 723410.38, "y": 6178373.7}},
    "postnummer": {"navn": "København N", "postnr": "2200"},
    "navngivenvejkommunedel": {"kommune": "0101", "vejkode": "3332"},
    "supplerendebynavn": {"navn": None},
}
ADDRESS = {
    "id_lokalid": ADDRESS_ID,
    "adressebetegnelse": "Jagtvej 120, 1. 101, 2200 København N",
    "etagebetegnelse": "1",
    "doerbetegnelse": "101",
    "status": "3",
    "husnummer": HOUSE,
}
BUILDING = {
    "id_lokalId": "0b8ee6fb-e2d2-4439-bab2-740ba7d12d4f",
    "status": "6",
    "husnummer": HOUSE_ID,
    "byg021BygningensAnvendelse": "150",
    "byg026Opfoerelsesaar": 1971,
    "byg056Varmeinstallation": "1",
    "byg057Opvarmningsmiddel": None,
    "byg404Koordinat": {"wkt": "POINT (723400.5 6178380.25)"},
}


def bbr_page(entity, nodes):
    return httpx.Response(
        200, json={"data": {entity: {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
    )


async def call(tool, **args):
    async with Client(mcp) as client:
        return await client.call_tool(tool, args)


def error_text(result):
    assert result.is_error
    return result.content[0].text


async def test_tools_are_registered():
    async with Client(mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == {"address_search", "address_get", "bbr_buildings", "bbr_units"}


@respx.mock
async def test_address_search():
    route = respx.get("https://adressevaelger.dk/adresser/soeg").mock(
        return_value=httpx.Response(200, json={"status": "ok", "fund": [
            {"type": "adresse", "id": ADDRESS_ID, "titel": "Jagtvej 120, 1. 101, 2200 København N",
             "husnummerId": HOUSE_ID}]})
    )
    result = await call("address_search", text="Jagtvej 120 1 101 2200")
    assert result.structured_content["results"] == [
        {"type": "adresse", "id": ADDRESS_ID, "titel": "Jagtvej 120, 1. 101, 2200 København N",
         "husnummer_id": HOUSE_ID}]
    params = route.calls[0].request.url.params
    assert params["tekst"] == "Jagtvej 120 1 101 2200" and params["token"] == "adressevaelger123"


@respx.mock
async def test_address_get_falls_back_to_husnummer():
    respx.get(f"https://adressevaelger.dk/adresser/{HOUSE_ID}").mock(return_value=httpx.Response(404))
    respx.get(f"https://adressevaelger.dk/husnumre/{HOUSE_ID}").mock(
        return_value=httpx.Response(200, json={"status": "ok", "husnummer": HOUSE})
    )
    out = (await call("address_get", id=HOUSE_ID)).structured_content
    assert out["level"] == "husnummer"
    assert out["betegnelse"] == "Jagtvej 120, 2200 København N"
    assert out["status"] == "gældende"
    assert out["kommunekode"] == "0101" and out["postnr"] == "2200"
    assert out["koordinater"] == {"x": 723410.38, "y": 6178373.7, "crs": "EPSG:25832"}
    assert "supplerende_bynavn" not in out


@respx.mock
async def test_bbr_buildings_by_address_resolves_husnummer_and_translates(api_key):
    respx.get(f"https://adressevaelger.dk/adresser/{ADDRESS_ID}").mock(
        return_value=httpx.Response(200, json={"status": "ok", "adresse": ADDRESS})
    )
    bbr = respx.post(BBR_URL).mock(return_value=bbr_page("BBR_Bygning", [BUILDING]))
    out = (await call("bbr_buildings", address_id=ADDRESS_ID)).structured_content
    assert json.loads(bbr.calls[0].request.content)["variables"]["where"] == {"husnummer": {"eq": HOUSE_ID}}
    building = out["results"][0]
    assert building["byg056Varmeinstallation"] == {"code": "1", "text": "Fjernvarme/blokvarme"}
    assert building["byg026Opfoerelsesaar"] == 1971
    assert building["byg404Koordinat"] == {"x": 723400.5, "y": 6178380.25, "crs": "EPSG:25832"}
    assert "byg057Opvarmningsmiddel" not in building
    assert out["count"] == 1 and "BBR" in out["source"]


@respx.mock
async def test_bbr_units_by_building(api_key):
    bbr = respx.post(BBR_URL).mock(return_value=bbr_page("BBR_Enhed", [{"id_lokalId": "u1", "status": "6",
                                                                         "enh020EnhedensAnvendelse": "140"}]))
    out = (await call("bbr_units", building_id=BUILDING["id_lokalId"])).structured_content
    sent = json.loads(bbr.calls[0].request.content)
    assert "BBR_Enhed(" in sent["query"]
    assert sent["variables"]["where"] == {"bygning": {"eq": BUILDING["id_lokalId"]}}
    assert out["results"][0]["enh020EnhedensAnvendelse"]["code"] == "140"


@respx.mock
async def test_buildings_not_standing_are_listed_apart(api_key):
    rows = [
        BUILDING,
        {"id_lokalId": "demolished", "status": "10", "byg021BygningensAnvendelse": "930"},
        {"id_lokalId": "shelved", "status": "14"},
        {"id_lokalId": "mistake", "status": "11"},
        {"id_lokalId": "partly", "status": "13"},
        {"id_lokalId": "odd", "status": "99"},
    ]
    respx.post(BBR_URL).mock(return_value=bbr_page("BBR_Bygning", rows))
    out = (await call("bbr_buildings", husnummer_id=HOUSE_ID)).structured_content
    assert out["count"] == 1 and [b["id_lokalId"] for b in out["results"]] == [BUILDING["id_lokalId"]]
    assert out["not_existing_count"] == 4
    states = {b["id_lokalId"]: b["state"] for b in out["not_existing"]}
    assert states == {"demolished": "demolished or otherwise ceased", "shelved": "shelved, never built",
                      "partly": "partly completed, may be partly in use (Delvis afsluttet)",
                      "odd": "unknown status"}
    assert out["not_existing"][0]["status"] == {"code": "10", "text": "Historisk"}
    assert out["registered_in_error_count"] == 1
    assert "Do not count" in out["note"]


@respx.mock
async def test_all_standing_has_no_not_existing_keys(api_key):
    respx.post(BBR_URL).mock(return_value=bbr_page("BBR_Bygning", [BUILDING]))
    out = (await call("bbr_buildings", husnummer_id=HOUSE_ID)).structured_content
    assert not {"not_existing", "not_existing_count", "note", "registered_in_error_count"} & out.keys()


async def test_needs_exactly_one_id():
    assert "exactly one" in error_text(await call("bbr_buildings"))
    assert "exactly one" in error_text(await call("bbr_units", address_id=ADDRESS_ID, unit_id=ADDRESS_ID))


async def test_rejects_malformed_ids():
    assert "UUID" in error_text(await call("bbr_units", unit_id="../husnumre/x"))
    assert "numeric" in error_text(await call("bbr_buildings", jordstykke_id="12a"))


@respx.mock
async def test_api_errors_come_back_as_tool_errors(api_key):
    respx.post(BBR_URL).mock(return_value=httpx.Response(401))
    assert "API key" in error_text(await call("bbr_units", unit_id=ADDRESS_ID))
