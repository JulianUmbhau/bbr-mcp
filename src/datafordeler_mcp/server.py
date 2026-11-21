"""MCP server for Danish buildings (BBR) and addresses (DAR).

Addresses come from Adressevælger (no key), buildings and units from the BBR register on Datafordeler's GraphQL service
(DATAFORDELER_API_KEY). Every BBR query asks for the data as registered and valid now.

`datafordeler-mcp --self-test` checks the server offline: the tools are registered and the code lists translate.
"""

import asyncio
import re
import sys
from typing import Any, Literal

import httpx
from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import adressevaelger, graphql
from .codes import code_lists, translate

mcp = MCPServer("datafordeler")

BBR = "BBR/v3"
BBR_SOURCE = (
    "Bygnings- og Boligregistret (BBR), via Datafordeler, CC BY 4.0. "
    "Grouping by status and the state labels are added by datafordeler-mcp."
)
DAR_SOURCE = "Danmarks Adresseregister (DAR), Klimadatastyrelsen, via Adressevælger, CC BY 4.0"
DAR_STATUS = {"2": "foreløbig", "3": "gældende", "4": "nedlagt", "5": "henlagt"}

# The statuses a BBR building or unit can have (BBR D0130 "Logisk Intern Datamodel" 1.8, section 3.2 Livscyklus).
# Only Opført means it stands. A demolished building is kept with status Historisk; Fejlregistreret marks one that
# was registered by mistake and never existed, so it is only counted.
# D0130 does not define Afsluttet, Midlertidig afsluttet or Delvis afsluttet (not even listed for buildings, but in
# use) for buildings. The labels for 9 and 13 are read from the data (Nov 2026 sample): no status-9 building has a
# year built (every Opført one has), and most status-13 ones have one, with heating. Status 12 is mixed: unclear.
STANDING = "6"
REGISTERED_IN_ERROR = "11"
STATES = {
    "2": "planned",
    "3": "under construction",
    "10": "demolished or otherwise ceased",
    "14": "shelved, never built",
    "9": "closed without being built (Afsluttet)",
    "12": "status unclear (Midlertidig afsluttet)",
    "13": "partly completed, may be partly in use (Delvis afsluttet)",
}
NOT_EXISTING_NOTE = (
    "not_existing lists what is registered here but does not stand now: planned, under construction, partly "
    "completed, demolished, shelved, closed without being built, or with an unclear status. Do not count these as "
    "existing. state is this server's reading; status is BBR's own value."
)

BUILDING_FIELDS = (
    "id_lokalId status kommunekode husnummer jordstykke grund byg007Bygningsnummer byg021BygningensAnvendelse "
    "byg026Opfoerelsesaar byg027OmTilbygningsaar byg030Vandforsyning byg031Afloebsforhold byg032YdervaeggensMateriale "
    "byg033Tagdaekningsmateriale byg036AsbestholdigtMateriale byg038SamletBygningsareal "
    "byg039BygningensSamledeBoligAreal byg040BygningensSamledeErhvervsAreal byg041BebyggetAreal byg054AntalEtager "
    "byg056Varmeinstallation byg057Opvarmningsmiddel byg058SupplerendeVarme byg070Fredning "
    "byg130ArealAfUdvendigEfterisolering byg404Koordinat { wkt }"
)
UNIT_FIELDS = (
    "id_lokalId status kommunekode bygning adresseIdentificerer etage opgang enh020EnhedensAnvendelse enh023Boligtype "
    "enh026EnhedensSamledeAreal enh027ArealTilBeboelse enh028ArealTilErhverv enh031AntalVaerelser enh032Toiletforhold "
    "enh033Badeforhold enh034Koekkenforhold enh035Energiforsyning enh045Udlejningsforhold enh051Varmeinstallation "
    "enh052Opvarmningsmiddel enh053SupplerendeVarme enh065AntalVandskylledeToiletter enh066AntalBadevaerelser"
)

UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
POINT = re.compile(r"POINT\s*\(\s*([-\d.]+)\s+([-\d.]+)\s*\)", re.I)


def _one_of(**ids: str | None) -> tuple[str, str]:
    given = {name: value.strip() for name, value in ids.items() if value and value.strip()}
    if len(given) != 1:
        raise ToolError(f"Give exactly one of: {', '.join(ids)}")
    name, value = next(iter(given.items()))
    if name == "jordstykke_id":
        if not value.isdigit():
            raise ToolError("jordstykke_id is the numeric id of the parcel (MAT jordstykke), e.g. 1234567")
    elif not UUID.match(value):
        raise ToolError(f"{name} must be a UUID")
    return name, value


def _point(wkt: str | None) -> dict[str, Any] | None:
    match = POINT.search(wkt or "")
    return {"x": float(match.group(1)), "y": float(match.group(2)), "crs": "EPSG:25832"} if match else None


def _row(row: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for field, value in row.items():
        if field == "byg404Koordinat":
            value = _point((value or {}).get("wkt"))
        if value is not None and value != "":
            out[field] = translate(field, value)
    return out


async def _bbr(entity: str, where: dict[str, Any], fields: str, limit: int) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        try:
            rows, truncated = await graphql.query(client, BBR, entity, where, fields, limit)
        except graphql.DatafordelerError as e:
            raise ToolError(str(e)) from None
    standing, not_existing, in_error = [], [], 0
    for row in rows:
        status = str(row.get("status"))
        if status == STANDING:
            standing.append(_row(row))
        elif status == REGISTERED_IN_ERROR:
            in_error += 1
        else:
            not_existing.append({"state": STATES.get(status, "unknown status"), **_row(row)})
    result: dict[str, Any] = {"count": len(standing), "results": standing}
    if not_existing:
        result |= {"not_existing_count": len(not_existing), "not_existing": not_existing, "note": NOT_EXISTING_NOTE}
    if in_error:
        result["registered_in_error_count"] = in_error
    result["source"] = BBR_SOURCE
    if truncated:
        result["truncated"] = f"more than {limit} matched; raise limit to see more"
    return result


async def _husnummer_of_address(address_id: str) -> str:
    async with httpx.AsyncClient() as client:
        try:
            address = await adressevaelger.get(client, "adresse", address_id)
        except adressevaelger.AdressevaelgerError as e:
            raise ToolError(str(e)) from None
    if address is None:
        raise ToolError(f"No DAR address {address_id}; if it is a husnummer (adgangsadresse) id, pass husnummer_id")
    return address["husnummer"]["id_lokalid"]


def _address(level: str, record: dict[str, Any]) -> dict[str, Any]:
    house = record["husnummer"] if level == "adresse" else record
    point = ((house.get("adgangspunkt") or {}).get("koordinater")) or {}
    road = house.get("navngivenvejkommunedel") or {}
    postcode = house.get("postnummer") or {}
    out = {
        "level": level,
        "id": record.get("id_lokalid"),
        "betegnelse": record.get("adressebetegnelse") or record.get("adgangsadressebetegnelse"),
        "etage": record.get("etagebetegnelse"),
        "doer": record.get("doerbetegnelse"),
        "status": DAR_STATUS.get(str(record.get("status")), record.get("status")),
        "husnummer_id": house.get("id_lokalid"),
        "vejnavn": house.get("vejnavn"),
        "husnummer": house.get("husnummertekst"),
        "vejkode": road.get("vejkode"),
        "kommunekode": road.get("kommune"),
        "postnr": postcode.get("postnr"),
        "postnrnavn": postcode.get("navn"),
        "supplerende_bynavn": (house.get("supplerendebynavn") or {}).get("navn"),
        "koordinater": {"x": point["x"], "y": point["y"], "crs": "EPSG:25832"} if point.get("x") else None,
        "source": DAR_SOURCE,
    }
    return {k: v for k, v in out.items() if v is not None}


@mcp.tool()
async def address_search(
    text: str, level: Literal["adresse", "husnummer"] = "adresse", limit: int = 10
) -> dict[str, Any]:
    """Find Danish addresses from free text, e.g. "Jagtvej 120, 2200" (spelling-tolerant; include the postcode).

    level "adresse" finds unit addresses (with floor and door), "husnummer" finds access addresses (one per entrance,
    what buildings are registered on). Each hit has the DAR id to pass to address_get, bbr_buildings or bbr_units.
    When the text is too vague the hits are roads (type "vejnavn" or "navngivenvejpostnummer"): add a house number.
    """
    if not text.strip():
        raise ToolError("text is empty")
    async with httpx.AsyncClient() as client:
        try:
            hits = await adressevaelger.search(client, text.strip(), level, max(1, min(limit, 100)))
        except adressevaelger.AdressevaelgerError as e:
            raise ToolError(str(e)) from None
    results = [
        {k: v for k, v in {"type": h.get("type"), "id": h.get("id"), "titel": h.get("titel"),
                           "husnummer_id": h.get("husnummerId")}.items() if v}
        for h in hits[:limit]
    ]
    return {"count": len(results), "results": results, "source": DAR_SOURCE}


@mcp.tool()
async def address_get(id: str) -> dict[str, Any]:
    """Look up one DAR address by id (a unit address or a husnummer/access address): the full address, postcode,
    municipality and road code, the husnummer id, and the entrance's coordinates (EPSG:25832, metres)."""
    _one_of(id=id)
    async with httpx.AsyncClient() as client:
        try:
            for level in ("adresse", "husnummer"):
                record = await adressevaelger.get(client, level, id.strip())
                if record is not None:
                    return _address(level, record)
        except adressevaelger.AdressevaelgerError as e:
            raise ToolError(str(e)) from None
    raise ToolError(f"No DAR address or husnummer with id {id}")


@mcp.tool()
async def bbr_buildings(
    husnummer_id: str | None = None,
    address_id: str | None = None,
    building_id: str | None = None,
    jordstykke_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """BBR buildings, by exactly one of: husnummer_id (DAR access address), address_id (DAR unit address; its
    husnummer is used), building_id (BBR bygning id) or jordstykke_id (cadastral parcel).

    Returns use (byg021), year built and rebuilt, areas, floors, materials, heating installation (byg056), heating
    medium (byg057), supplementary heat (byg058), listing status and the coordinates (EPSG:25832). Coded fields come
    as {"code", "text"}; empty fields are left out. Outbuildings (garages, sheds) are buildings too.

    results and count are the buildings standing now. Planned, under-construction, partly completed, demolished and
    shelved buildings are listed apart in not_existing, each with its state; never count those as existing buildings.
    """
    name, value = _one_of(
        husnummer_id=husnummer_id, address_id=address_id, building_id=building_id, jordstykke_id=jordstykke_id
    )
    if name == "address_id":
        name, value = "husnummer_id", await _husnummer_of_address(value)
    field = {"husnummer_id": "husnummer", "building_id": "id_lokalId", "jordstykke_id": "jordstykke"}[name]
    return await _bbr("BBR_Bygning", {field: {"eq": value}}, BUILDING_FIELDS, max(1, min(limit, 1000)))


@mcp.tool()
async def bbr_units(
    address_id: str | None = None,
    building_id: str | None = None,
    unit_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """BBR units (enheder: dwellings, businesses), by exactly one of: address_id (DAR unit address),
    building_id (all units in a BBR building) or unit_id (BBR enhed id).

    Returns use (enh020), housing type, areas, rooms, kitchen/bath/toilet, energy supply, rental status and the unit's
    own heating (enh051-053). Coded fields come as {"code", "text"}; empty fields are left out.

    results and count are the units that exist now; planned, removed and shelved units are listed apart in
    not_existing, each with its state.
    """
    name, value = _one_of(address_id=address_id, building_id=building_id, unit_id=unit_id)
    field = {"address_id": "adresseIdentificerer", "building_id": "bygning", "unit_id": "id_lokalId"}[name]
    return await _bbr("BBR_Enhed", {field: {"eq": value}}, UNIT_FIELDS, max(1, min(limit, 1000)))


async def _self_test() -> None:
    async with Client(mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    expected = {"address_search", "address_get", "bbr_buildings", "bbr_units"}
    if names != expected:
        sys.exit(f"self-test failed: tools {sorted(names)}, expected {sorted(expected)}")
    heating = translate("byg056Varmeinstallation", "1")
    if heating != {"code": "1", "text": "Fjernvarme/blokvarme"}:
        sys.exit(f"self-test failed: byg056 code 1 translated to {heating}")
    print(f"self-test ok: {len(names)} tools, {len(code_lists())} code lists", file=sys.stderr)


def main() -> None:
    if sys.argv[1:] == ["--self-test"]:
        asyncio.run(_self_test())
        return
    mcp.run()


if __name__ == "__main__":
    main()
