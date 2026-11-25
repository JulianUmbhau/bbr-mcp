"""Calls the real services: `uv run pytest -m live` with DATAFORDELER_API_KEY set."""

import os

import pytest
from mcp import Client

from bbr_mcp.server import mcp

pytestmark = [
    pytest.mark.live,
    pytest.mark.anyio,
    pytest.mark.skipif(not os.environ.get("DATAFORDELER_API_KEY"), reason="DATAFORDELER_API_KEY not set"),
]

# Jagtvej 120, 2200 København N: a husnummer with several buildings and many units
HOUSE_ID = "0a3f507a-9d13-32b8-e044-0003ba298018"
ADDRESS_ID = "0a3f509e-fb66-32b8-e044-0003ba298018"


async def call(tool, **args):
    async with Client(mcp) as client:
        result = await client.call_tool(tool, args)
    assert not result.is_error, result.content
    return result.structured_content


def ids(out, key="id_lokalId"):
    return {r.get(key) for r in out["results"] + out.get("not_existing", [])}


async def test_search_then_get():
    hits = (await call("address_search", text="Jagtvej 120, 1. 101, 2200"))["results"]
    assert ADDRESS_ID in {h["id"] for h in hits}
    address = await call("address_get", id=ADDRESS_ID)
    assert address["husnummer_id"] == HOUSE_ID and address["postnr"] == "2200"


async def test_a_flat_gives_its_own_building_among_those_at_its_entrance():
    by_house = await call("buildings", husnummer_id=HOUSE_ID)
    by_address = await call("buildings", address_id=ADDRESS_ID)
    assert by_house["count"] >= 1 and by_address["found_via"] == "unit" and by_address["count"] == 1
    assert ids(by_address) <= ids(by_house)
    assert all(b["status"]["code"] == "6" for b in by_house["results"])
    uses = [b["byg021BygningensAnvendelse"] for b in by_house["results"]]
    assert all(u["text"] != "unknown code" for u in uses)


async def test_units_by_address_and_by_building():
    units = await call("units", address_id=ADDRESS_ID)
    assert units["count"] >= 1
    building_id = units["results"][0]["bygning"]
    in_building = await call("units", building_id=building_id, limit=1000)
    assert "truncated" not in in_building
    assert ADDRESS_ID in ids(in_building, "adresseIdentificerer")


async def test_area_around_an_address_with_filters():
    everything = await call("buildings_in_area", around_address_id=ADDRESS_ID, radius_m=300, limit=0)
    assert everything["count"] > 10 and everything["results"] == []
    houses = await call("buildings_in_area", around_address_id=ADDRESS_ID, radius_m=300, uses=["140"])
    assert 0 < houses["count"] < everything["count"]
    assert {u["code"] for u in houses["summary"]["by_use"]} == {"140"}


async def test_area_in_wgs84_finds_the_same_as_in_utm32():
    in_wgs84 = await call("buildings_in_area", bbox=[12.551, 55.698, 12.555, 55.700], crs="4326", limit=0)
    assert in_wgs84["count"] > 0


async def test_list_of_addresses():
    out = await call(
        "buildings_for_addresses",
        addresses=[
            "Jagtvej 120, 1. 101, 2200 København N",
            "Rådhuspladsen 1, 1550 København V",
            "Ikkeeksisterendevej 999, 9999",
        ],
        include_units=True,
    )
    s = out["summary"]
    assert s["matched"] == 2 and s["not_found"] + s["vague"] == 1
    jagtvej = out["results"][0]
    assert jagtvej["match"]["husnummer_id"] == HOUSE_ID and jagtvej["building_count"] >= 1
    assert jagtvej["units"][0]["adresseIdentificerer"] == ADDRESS_ID


async def test_building_by_id_and_parcel():
    building = (await call("buildings", husnummer_id=HOUSE_ID))["results"][0]
    same = await call("buildings", building_id=building["id_lokalId"])
    assert same["results"][0]["id_lokalId"] == building["id_lokalId"]
    if building.get("jordstykke"):
        on_parcel = await call("buildings", jordstykke_id=building["jordstykke"])
        assert building["id_lokalId"] in ids(on_parcel)


async def test_buildings_for_building_ids_match_the_husnummer():
    by_house = await call("buildings", husnummer_id=HOUSE_ID)
    building_ids = sorted(ids(by_house))
    out = await call("buildings_for_addresses", building_ids=building_ids, include_units=True)
    assert out["summary"]["matched"] == len(building_ids) and out["summary"]["not_found"] == 0
    assert out["summary"]["buildings"] == by_house["count"]
    assert any(e.get("units") for e in out["results"])


# Kommandanthøjen 3, 3700 Rønne: an entrance to a block whose representative husnummer is another one.
KOMMANDANTHOEJEN_3 = "0a3f5087-79a7-32b8-e044-0003ba298018"
KOMMANDANTHOEJEN_3_1_4 = "0a3f50b1-fc3b-32b8-e044-0003ba298018"


async def test_an_entrance_and_a_flat_find_their_building():
    by_entrance = await call("buildings", husnummer_id=KOMMANDANTHOEJEN_3)
    assert by_entrance["found_via"] == "entrance (opgang)" and by_entrance["count"] == 1
    by_flat = await call("buildings", address_id=KOMMANDANTHOEJEN_3_1_4, include_units=True)
    assert by_flat["found_via"] == "unit" and len(by_flat["units"]) == 1
    assert by_flat["results"][0]["id_lokalId"] == by_entrance["results"][0]["id_lokalId"]


async def test_the_three_kinds_of_property():
    land = await call("buildings", bfe_nummer=5409706, limit=1000)
    assert "samlet fast ejendom" in land["property"]["kind"] and land["count"] >= 2
    flat = await call("buildings", bfe_nummer=415881, include_units=True)
    assert "ejerlejlighed" in flat["property"]["kind"] and flat["units"]
    hut = await call("buildings", bfe_nummer=704266)
    assert "fremmed grund" in hut["property"]["kind"]


async def test_solar_plants_around_roenne():
    out = await call(
        "technical_installations", center=[14.70, 55.10], crs="4326", radius_m=2500, types=["1230"], limit=0
    )
    solar = out["summary"]["by_type"][0]
    assert solar["code"] == "1230" and solar["count"] > 50 and solar["power"]["median"] < 50
