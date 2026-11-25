"""The list tools: buildings in an area, and buildings for a list of addresses."""

import json

import httpx2
import mock_http as respx
import pytest
from fake_bbr import FakeBBR
from mcp import Client

from bbr_mcp.server import mcp

pytestmark = pytest.mark.anyio

BBR_URL = "https://graphql.datafordeler.dk/BBR/v3"
SEARCH_URL = "https://adressevaelger.dk/adresser/soeg"
H1, H2 = "0a3f507a-9d13-32b8-e044-0003ba298018", "11111111-2222-3333-4444-555555555555"
A1 = "0a3f509e-fb66-32b8-e044-0003ba298018"
B1, B2, B3 = (f"{i}a3f507a-0000-4000-8000-000000000000" for i in (1, 2, 3))


def page(entity, nodes):
    return httpx2.Response(200, json={"data": {entity: {"nodes": nodes, "pageInfo": {"hasNextPage": False}}}})


def building(id, house=H1, use="120", heating="5", medium="1", year=1975, status="6", area=150):
    return {
        "id_lokalId": id,
        "status": status,
        "husnummer": house,
        "byg021BygningensAnvendelse": use,
        "byg026Opfoerelsesaar": year,
        "byg038SamletBygningsareal": area,
        "byg039BygningensSamledeBoligAreal": area,
        "byg056Varmeinstallation": heating,
        "byg057Opvarmningsmiddel": medium,
        "byg058SupplerendeVarme": None,
        "byg404Koordinat": {"wkt": "POINT (723400 6178380)"},
    }


async def call(tool, **args):
    async with Client(mcp) as client:
        return await client.call_tool(tool, args)


def error_text(result):
    assert result.is_error
    return result.content[0].text


def sent(route, i=0):
    return json.loads(route.calls[i].request.content)["variables"]


# ------------------------------------------------------------------ area


@respx.mock
async def test_area_sends_the_polygon_filters_and_status_and_summarises(api_key):
    rows = [
        building("b1"),
        building("b2", heating="1", medium=None, year=2016),
        building("b3", use="930", heating=None, medium=None, year=None),
    ]
    route = respx.post(BBR_URL).mock(return_value=page("BBR_Bygning", rows))
    out = (
        await call(
            "buildings_in_area",
            center=[723410, 6178373],
            radius_m=200,
            uses=["120", "Udhus"],
            limit=2,
        )
    ).structured_content
    filters = sent(route)["where"]["and"]
    assert filters[0]["byg404Koordinat"]["intersects"]["crs"] == 25832
    assert filters[0]["byg404Koordinat"]["intersects"]["wkt"].startswith("POLYGON((723610.00 6178373.00")
    assert {"byg021BygningensAnvendelse": {"in": ["120", "930"]}} in filters
    assert {"status": {"eq": "6"}} in filters
    s = out["summary"]
    assert s["buildings"] == 3 and out["count"] == 3
    assert {"code": "5", "text": "Varmepumpe", "count": 1} in s["by_heating_installation"]
    assert {"code": None, "text": "not registered", "count": 1} in s["by_heating_installation"]
    years = s["by_year_built"]
    assert {"years": "1950-1979", "count": 1} in years and {"years": "unknown", "count": 1} in years
    assert s["total_building_area_m2"] == 450
    assert len(out["results"]) == 2 and "results_truncated" in out
    assert out["area"]["km2"] == pytest.approx(0.125, rel=0.02)


@respx.mock
async def test_area_heating_filters_apply_to_what_comes_back(api_key):
    rows = [
        building("hp", heating="5", medium="1"),
        building("district", heating="1", medium=None),
        building("oil", heating="2", medium="3"),
        building("flats", heating="99", medium=None),
    ]
    route = respx.post(BBR_URL).mock(return_value=page("BBR_Bygning", rows))
    out = (
        await call(
            "buildings_in_area",
            bbox=[723000, 6178000, 724000, 6179000],
            heating=["Varmepumpe", "2"],
            heating_medium=["1"],
        )
    ).structured_content
    assert not any(
        "byg056Varmeinstallation" in f or "byg057Opvarmningsmiddel" in f for f in sent(route)["where"]["and"]
    )
    assert [b["id_lokalId"] for b in out["results"]] == ["hp"]
    assert out["summary"]["buildings"] == 1
    on_units = (
        await call("buildings_in_area", bbox=[723000, 6178000, 724000, 6179000], heating=["99"])
    ).structured_content
    assert [b["id_lokalId"] for b in on_units["results"]] == ["flats"]


@respx.mock
async def test_area_with_not_existing_splits_them(api_key):
    route = respx.post(BBR_URL).mock(return_value=page("BBR_Bygning", [building("b1"), building("gone", status="10")]))
    out = (
        await call("buildings_in_area", bbox=[723000, 6178000, 724000, 6179000], include_not_existing=True)
    ).structured_content
    assert {"status": {"eq": "6"}} not in sent(route)["where"]["and"]
    assert out["count"] == 1 and out["not_existing_by_state"] == {"demolished or otherwise ceased": 1}


@respx.mock
async def test_area_around_an_address(api_key):
    respx.get(f"https://adressevaelger.dk/adresser/{A1}").mock(
        return_value=httpx2.Response(
            200,
            json={
                "status": "ok",
                "adresse": {
                    "id_lokalid": A1,
                    "husnummer": {"id_lokalid": H1, "adgangspunkt": {"koordinater": {"x": 723410.38, "y": 6178373.7}}},
                },
            },
        )
    )
    route = respx.post(BBR_URL).mock(return_value=page("BBR_Bygning", []))
    out = (await call("buildings_in_area", around_address_id=A1, radius_m=100)).structured_content
    assert "723510.38 6178373.70" in sent(route)["where"]["and"][0]["byg404Koordinat"]["intersects"]["wkt"]
    assert out["count"] == 0


@respx.mock
async def test_area_too_many_buildings(api_key, monkeypatch):
    monkeypatch.setattr("bbr_mcp.server.MAX_AREA_BUILDINGS", 1)
    respx.post(BBR_URL).mock(
        return_value=httpx2.Response(
            200,
            json={
                "data": {
                    "BBR_Bygning": {"nodes": [building("b1")], "pageInfo": {"hasNextPage": True, "endCursor": "c"}}
                }
            },
        )
    )
    assert "smaller area" in error_text(await call("buildings_in_area", bbox=[723000, 6178000, 724000, 6179000]))


@pytest.mark.parametrize(
    "args, message",
    [
        ({}, "exactly one area"),
        ({"center": [723410, 6178373]}, "needs radius_m"),
        ({"bbox": [700000, 6100000, 720000, 6120000]}, "km²"),
        ({"bbox": [723000, 6178000, 724000, 6179000], "heating": ["Atomkraft"]}, "not a code"),
        ({"bbox": [723000, 6178000, 724000, 6179000], "uses": ["hus"]}, "did you mean"),
        ({"around_address_id": "x", "radius_m": 10}, "UUID"),
    ],
)
async def test_area_input_errors(args, message):
    assert message in error_text(await call("buildings_in_area", **args))


# ------------------------------------------------------------------ list of addresses


def hits(*found):
    return httpx2.Response(200, json={"status": "ok", "fund": list(found)})


@respx.mock
async def test_addresses_are_matched_and_looked_up_together(api_key):
    def search(request):
        text = request.url.params["tekst"]
        if text.startswith("Jagtvej"):
            return hits(
                {"type": "adresse", "id": A1, "titel": "Jagtvej 120, 1. 101, 2200 København N", "husnummerId": H1}
            )
        if text.startswith("Nørrebro"):
            return hits(
                {"type": "husnummer", "id": H2, "titel": "Nørrebrogade 1, 2200 København N"},
                {"type": "husnummer", "id": "x", "titel": "Nørrebrogade 11, 2200 København N"},
            )
        if text.startswith("Vej"):
            return hits({"type": "vejnavn", "titel": "Vejen"})
        return hits()

    respx.get(SEARCH_URL).mock(side_effect=search)
    fake = FakeBBR(
        BBR_Enhed=[{"id_lokalId": "u1", "status": "6", "adresseIdentificerer": A1, "bygning": "b1"}],
        BBR_Bygning=[
            building("b1"),
            building("b1-shed", use="930", heating=None),
            building("b2", house=H2, heating="1"),
            building("gone", house=H2, status="10"),
        ],
    )
    respx.post(BBR_URL).mock(side_effect=fake)
    out = (
        await call(
            "buildings_for_addresses",
            addresses=["Jagtvej 120, 1. 101, 2200 København N", "Nørrebrogade 1 2200", "Vej", "Fake Street 9"],
        )
    ).structured_content
    assert fake.asked("BBR_Enhed") == [{"adresseIdentificerer": {"in": [A1]}}]
    assert fake.asked("BBR_Bygning")[-1] == {"husnummer": {"in": [H2]}}
    s = out["summary"]
    assert (s["entries"], s["matched"], s["matched_exactly"], s["not_found"], s["vague"]) == (4, 2, 1, 1, 1)
    assert s["buildings"] == 2
    jagtvej, norrebro, vague, fake_street = out["results"]
    assert jagtvej["match"]["exact"] is True and jagtvej["match"]["adresse_id"] == A1
    # The flat's own building, through its unit: not the shed at the same entrance.
    assert jagtvej["found_via"] == "unit" and [b["id_lokalId"] for b in jagtvej["buildings"]] == ["b1"]
    assert norrebro["match"]["exact"] is False and norrebro["match"]["alternatives"] == [
        "Nørrebrogade 11, 2200 København N"
    ]
    assert norrebro["found_via"] == "husnummer"
    assert norrebro["building_count"] == 1 and norrebro["not_existing_count"] == 1 and "not_existing" not in norrebro
    assert vague["status"] == "vague" and "house number" in vague["hint"]
    assert fake_street["status"] == "not_found"


@respx.mock
async def test_husnummer_ids_skip_the_search_and_batch_by_100(api_key):
    ids = [f"00000000-0000-0000-0000-{i:012d}" for i in range(150)]
    fake = FakeBBR()
    respx.post(BBR_URL).mock(side_effect=fake)
    out = (await call("buildings_for_addresses", husnummer_ids=ids)).structured_content
    assert sorted(len(w["husnummer"]["in"]) for w in fake.asked("BBR_Bygning")) == [50, 100]
    # None has a building of its own, so each is looked up as an entrance, in batches too.
    assert sorted(len(w["adgangFraHusnummer"]["in"]) for w in fake.asked("BBR_Opgang")) == [50, 100]
    assert out["summary"]["matched"] == 150 and out["summary"]["buildings"] == 0


@respx.mock
async def test_units_for_addresses_with_floor_and_door(api_key):
    respx.get(SEARCH_URL).mock(
        return_value=hits(
            {"type": "adresse", "id": A1, "titel": "Jagtvej 120, 1. 101, 2200 København N", "husnummerId": H1}
        )
    )

    unit = {
        "id_lokalId": "u1",
        "status": "6",
        "adresseIdentificerer": A1,
        "bygning": "b1",
        "enh020EnhedensAnvendelse": "150",
        "enh051Varmeinstallation": "1",
    }
    respx.post(BBR_URL).mock(
        side_effect=FakeBBR(BBR_Enhed=[unit], BBR_Bygning=[building("b1", use="150", heating="99")])
    )
    out = (
        await call("buildings_for_addresses", addresses=["Jagtvej 120, 1. 101, 2200"], include_units=True)
    ).structured_content
    unit = out["results"][0]["units"][0]
    assert unit["enh051Varmeinstallation"] == {"code": "1", "text": "Fjernvarme/blokvarme"}


@respx.mock
async def test_a_large_site_lists_at_most_per_address_limit(api_key):
    respx.post(BBR_URL).mock(
        return_value=page("BBR_Bygning", [building(f"b{i}") for i in range(30)] + [building("gone", status="10")])
    )
    out = (await call("buildings_for_addresses", husnummer_ids=[H1], per_address_limit=5)).structured_content
    site = out["results"][0]
    assert (
        site["building_count"] == 30
        and len(site["buildings"]) == 5
        and "30 standing, 5 listed" in site["buildings_truncated"]
    )
    assert site["not_existing_count"] == 1 and out["summary"]["buildings"] == 30


@respx.mock
async def test_buildings_for_building_ids_with_units(api_key):
    fake = FakeBBR(
        BBR_Enhed=[{"id_lokalId": "u1", "status": "6", "bygning": B1, "enh020EnhedensAnvendelse": "120"}],
        BBR_Bygning=[building(B1), building(B2, status="10")],
    )
    respx.post(BBR_URL).mock(side_effect=fake)
    out = (await call("buildings_for_addresses", building_ids=[B1, B2, B3, B1], include_units=True)).structured_content
    assert fake.asked("BBR_Bygning") == [{"id_lokalId": {"in": [B1, B2, B3]}}]
    assert fake.asked("BBR_Enhed") == [{"bygning": {"in": [B1, B2]}}]
    s = out["summary"]
    assert (s["entries"], s["matched"], s["not_found"], s["buildings"]) == (4, 3, 1, 1)
    first, demolished, missing, again = out["results"]
    assert first["building_count"] == 1 and first["units"][0]["id_lokalId"] == "u1"
    assert (
        demolished["building_count"] == 0
        and demolished["state"] == "demolished or otherwise ceased"
        and demolished["not_existing_count"] == 1
    )
    assert missing == {"input": B3, "status": "not_found"}
    assert again["match"]["building_id"] == B1


@pytest.mark.parametrize(
    "args, message",
    [
        ({}, "exactly one of addresses, husnummer_ids, building_ids, bfe_numbers or street"),
        ({"bfe_numbers": [0]}, "positive"),
        ({"addresses": ["a"], "husnummer_ids": [H1]}, "exactly one of"),
        ({"husnummer_ids": [H1], "building_ids": [B1]}, "exactly one of"),
        ({"building_ids": [B1] * 201}, "At most 200"),
        ({"building_ids": ["../x"]}, "UUID"),
        ({"husnummer_ids": [H1], "per_address_limit": -1}, "0 or more"),
        ({"addresses": [f"Vej {i}" for i in range(201)]}, "At most 200"),
        ({"husnummer_ids": ["not-a-uuid"]}, "UUID"),
    ],
)
async def test_address_list_input_errors(args, message):
    assert message in error_text(await call("buildings_for_addresses", **args))


def test_exact_keeps_house_number_and_floor_apart():
    from bbr_mcp.server import _normal

    assert _normal("Nørregade 1, 11. tv., 5000 Odense C") != _normal("Nørregade 11, 1. tv., 5000 Odense C")
    assert _normal("Jagtvej 120, 1. 101, 2200 København N") == _normal("Jagtvej 120 1 101 2200 københavn n")
    assert _normal("Vestergade 12A, 7100") == _normal("Vestergade 12 A, 7100")


# ------------------------------------------------------------------ a street


ROADS_URL = "https://adressevaelger.dk/soeg"
HOUSES_URL = "https://adressevaelger.dk/husnumre/soeg"


def road(name, postcode, town, count):
    return {
        "type": "navngivenvejpostnummer",
        "id": f"r-{postcode}",
        "titel": f"{name} {postcode} {town}",
        "vejnavn": name,
        "postnr": postcode,
        "antal_husnumre": count,
    }


def house(id, name, number, postcode="4220", town="Korsør"):
    titel = f"{name} {number}, {postcode} {town}"
    return {"type": "husnummer", "id": id, "titel": titel, "vejnavn": name, "husnummer": number}


@respx.mock
async def test_a_street_gives_every_entrance_and_its_buildings_in_one_call(api_key):
    roads = respx.get(ROADS_URL).mock(
        return_value=httpx2.Response(
            200,
            json={
                "status": "ok",
                "fund": [road("Søvænget", "4220", "Korsør", 2), road("Søvænget", "2770", "Kastrup", 30)],
            },
        )
    )
    houses = respx.get(HOUSES_URL).mock(
        return_value=httpx2.Response(
            200,
            json={
                "status": "ok",
                "fund": [house(H1, "Søvænget", "2A"), house(H2, "Søvænget", "2B"), house("x", "Søvængerne", "1")],
            },
        )
    )
    bbr = FakeBBR(BBR_Bygning=[building(B1, house=H1), building(B2, house=H2, use="910")])
    respx.post(BBR_URL).mock(side_effect=bbr)
    out = (await call("buildings_for_addresses", street="Søvænget, 4220 Korsør")).structured_content
    assert roads.calls[0].request.url.params["maal"] == "navngivenvejpostnummer"
    params = houses.calls[0].request.url.params
    assert (params["vejnavn"], params["postnummer"], params["maksimum"]) == ("Søvænget", "4220", "100")
    assert out["summary"]["street"] == {"titel": "Søvænget 4220 Korsør", "entrances": 2, "listed": 2, "complete": True}
    assert [e["input"] for e in out["results"]] == ["Søvænget 2A, 4220 Korsør", "Søvænget 2B, 4220 Korsør"]
    assert out["summary"]["entries"] == 2 and out["summary"]["buildings"] == 2
    assert "DAR" in out["source"]


@respx.mock
async def test_a_long_street_says_what_is_missing(api_key):
    respx.get(ROADS_URL).mock(
        return_value=httpx2.Response(200, json={"status": "ok", "fund": [road("Jagtvej", "2200", "København N", 187)]})
    )
    respx.get(HOUSES_URL).mock(
        return_value=httpx2.Response(
            200, json={"status": "ok", "fund": [house(H1, "Jagtvej", "1", "2200", "København N")]}
        )
    )
    respx.post(BBR_URL).mock(side_effect=FakeBBR(BBR_Bygning=[building(B1)]))
    out = await call("buildings_for_addresses", street="Jagtvej 2200", per_address_limit=0)
    street = out.structured_content["summary"]["street"]
    assert street["complete"] is False and street["entrances"] == 187 and street["listed"] == 1
    assert "at most 100" in street["note"] and "buildings_in_area" in street["note"]


@respx.mock
async def test_a_street_without_postcode_or_unknown_is_refused(api_key):
    respx.get(ROADS_URL).mock(
        side_effect=[
            httpx2.Response(
                200,
                json={
                    "status": "ok",
                    "fund": [road("Søvænget", "4220", "Korsør", 9), road("Søvænget", "2770", "Kastrup", 30)],
                },
            ),
            httpx2.Response(200, json={"status": "ok", "fund": []}),
        ]
    )
    assert "Several roads match" in error_text(await call("buildings_for_addresses", street="Søvænget"))
    assert "No road matches" in error_text(await call("buildings_for_addresses", street="Nonexistentvej, 4220"))
    both = await call("buildings_for_addresses", street="Søvænget, 4220", husnummer_ids=[H1])
    assert "exactly one of" in error_text(both)
