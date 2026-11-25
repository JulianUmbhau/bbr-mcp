"""How BBR's links are followed: entrances (opgang), units, properties (BFE) and technical installations."""

import httpx2
import mock_http as respx
import pytest
from fake_bbr import FakeBBR
from mcp import Client

from bbr_mcp.server import mcp

pytestmark = pytest.mark.anyio

BBR_URL = "https://graphql.datafordeler.dk/BBR/v3"
REP, OTHER = "0a3f5087-79a5-32b8-e044-0003ba298018", "0a3f5087-79a7-32b8-e044-0003ba298018"
FLAT = "0a3f50b1-fc3b-32b8-e044-0003ba298018"
B7 = "5b01ad50-02c3-4086-8e55-d2a82f039d39"


def building(id, house=REP, status="6", **extra):
    return {"id_lokalId": id, "status": status, "husnummer": house, "byg021BygningensAnvendelse": "140", **extra}


async def call(tool, **args):
    async with Client(mcp) as client:
        return await client.call_tool(tool, args)


@respx.mock
async def test_a_husnummer_that_is_another_entrance_finds_its_building(api_key):
    fake = FakeBBR(
        BBR_Bygning=[building(B7)],
        BBR_Opgang=[{"adgangFraHusnummer": OTHER, "bygning": B7, "status": "6"}],
    )
    respx.post(BBR_URL).mock(side_effect=fake)
    out = (await call("buildings", husnummer_id=OTHER)).structured_content
    assert out["found_via"] == "entrance (opgang)" and [b["id_lokalId"] for b in out["results"]] == [B7]
    assert fake.asked("BBR_Opgang") == [{"adgangFraHusnummer": {"in": [OTHER]}}]


@respx.mock
async def test_an_address_without_a_unit_falls_back_to_its_entrance(api_key):
    respx.get(f"https://adressevaelger.dk/adresser/{FLAT}").mock(
        return_value=httpx2.Response(200, json={"status": "ok", "adresse": {"husnummer": {"id_lokalid": OTHER}}})
    )
    fake = FakeBBR(BBR_Bygning=[building(B7)], BBR_Opgang=[{"adgangFraHusnummer": OTHER, "bygning": B7}])
    respx.post(BBR_URL).mock(side_effect=fake)
    out = (await call("buildings", address_id=FLAT)).structured_content
    assert out["found_via"] == "entrance (opgang)" and out["count"] == 1 and out["husnummer_id"] == OTHER


@respx.mock
async def test_nothing_found_says_so(api_key):
    respx.post(BBR_URL).mock(side_effect=FakeBBR())
    out = (await call("buildings", husnummer_id=OTHER)).structured_content
    assert out["found_via"] == "none" and out["count"] == 0 and "No building" in out["note_found"]


def properties():
    return FakeBBR(
        BBR_Ejendomsrelation=[
            {"id_lokalId": "r-land", "bfeNummer": 5409706, "samletFastEjendom": "5409706"},
            {"id_lokalId": "r-flat", "bfeNummer": 415881, "ejerlejlighed": "415881"},
            {"id_lokalId": "r-hut", "bfeNummer": 704266, "bygningPaaFremmedGrund": "704266"},
        ],
        BBR_Grund=[
            {"id_lokalId": "g1", "bestemtFastEjendom": "r-land"},
            {"id_lokalId": "g2", "bestemtFastEjendom": "r-land"},
        ],
        BBR_Bygning=[
            building("b1", grund="g1"),
            building("b2", grund="g2"),
            building("b-flat"),
            building("b-hut", byg021BygningensAnvendelse="540"),
        ],
        BBR_EnhedEjendomsrelation=[{"enhed": "u1", "ejerlejlighed": "r-flat"}],
        BBR_Enhed=[{"id_lokalId": "u1", "status": "6", "bygning": "b-flat", "enh026EnhedensSamledeAreal": 111}],
        BBR_BygningEjendomsrelation=[{"bygning": "b-hut", "bygningPaaFremmedGrund": "r-hut"}],
        BBR_TekniskAnlaeg=[
            {"id_lokalId": "t-roof", "status": "6", "tek020Klassifikation": "1230", "tek039Effekt": 6, "bygning": "b1"},
            {
                "id_lokalId": "t-field",
                "status": "6",
                "tek020Klassifikation": "1230",
                "tek039Effekt": 71100,
                "grund": "g2",
            },
            {"id_lokalId": "t-tank", "status": "6", "tek020Klassifikation": "1110", "grund": "g1"},
            {"id_lokalId": "t-gone", "status": "10", "tek020Klassifikation": "1210", "grund": "g1"},
        ],
    )


@respx.mock
async def test_a_property_gives_every_building_on_its_land(api_key):
    respx.post(BBR_URL).mock(side_effect=properties())
    out = (await call("buildings", bfe_nummer=5409706)).structured_content
    assert out["found_via"] == "bfe" and out["property"]["grunde"] == 2
    assert "samlet fast ejendom" in out["property"]["kind"]
    assert sorted(b["id_lokalId"] for b in out["results"]) == ["b1", "b2"]


@respx.mock
async def test_an_owner_flat_gives_its_unit_and_building(api_key):
    respx.post(BBR_URL).mock(side_effect=properties())
    out = (await call("buildings", bfe_nummer=415881, include_units=True)).structured_content
    assert "ejerlejlighed" in out["property"]["kind"]
    assert [b["id_lokalId"] for b in out["results"]] == ["b-flat"]
    assert [u["id_lokalId"] for u in out["units"]] == ["u1"]


@respx.mock
async def test_properties_in_a_list(api_key):
    respx.post(BBR_URL).mock(side_effect=properties())
    out = (await call("buildings_for_addresses", bfe_numbers=[5409706, 704266, 999])).structured_content
    land, hut, missing = out["results"]
    assert land["building_count"] == 2 and hut["building_count"] == 1 and "fremmed grund" in hut["property_kind"]
    assert missing["status"] == "not_found"
    assert out["summary"]["buildings"] == 3 and out["summary"]["not_found"] == 1


@respx.mock
async def test_technical_installations_on_a_property_include_its_buildings(api_key):
    fake = properties()
    respx.post(BBR_URL).mock(side_effect=fake)
    out = (await call("technical_installations", bfe_nummer=5409706, types=["Solcelleanlæg"])).structured_content
    assert sorted(r["id_lokalId"] for r in out["results"]) == ["t-field", "t-roof"]
    solar = out["summary"]["by_type"][0]
    assert solar["text"] == "Solcelleanlæg" and solar["count"] == 2
    assert solar["power"] == {"registered": 2, "median": 71100, "max": 71100, "total": 71106}
    assert "W" in out["summary"]["note"]
    asked = fake.asked("BBR_TekniskAnlaeg")
    assert {next(iter(w["and"][0])) for w in asked} == {"grund", "bygning"}
    assert all({"tek020Klassifikation": {"in": ["1230"]}} in w["and"] for w in asked)


@respx.mock
async def test_technical_installations_not_existing_are_counted_apart(api_key):
    respx.post(BBR_URL).mock(side_effect=properties())
    out = (await call("technical_installations", bfe_nummer=5409706, include_not_existing=True)).structured_content
    assert out["count"] == 3 and out["not_existing_by_state"] == {"demolished or otherwise ceased": 1}


@respx.mock
async def test_technical_installations_in_an_area_filter_on_their_coordinates(api_key):
    fake = FakeBBR()
    respx.post(BBR_URL).mock(side_effect=fake)
    out = (await call("technical_installations", center=[723400, 6178380], radius_m=500)).structured_content
    (where,) = fake.asked("BBR_TekniskAnlaeg")
    assert "tek109Koordinat" in where["and"][0] and {"status": {"eq": "6"}} in where["and"]
    assert out["count"] == 0 and out["area"]["km2"] > 0.7


@pytest.mark.parametrize(
    "args, message",
    [
        ({}, "exactly one"),
        ({"bfe_nummer": 1, "husnummer_id": OTHER}, "exactly one"),
        ({"building_id": B7, "bbox": [1, 2, 3, 4]}, "exactly one"),
        ({"bfe_nummer": 1, "types": ["Varmepumpe"]}, "not a code"),
    ],
)
async def test_technical_installation_input_errors(args, message):
    result = await call("technical_installations", **args)
    assert result.is_error and message in result.content[0].text
