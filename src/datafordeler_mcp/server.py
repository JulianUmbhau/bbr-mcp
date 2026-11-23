"""MCP server for Danish buildings (BBR) and addresses (DAR).

Addresses come from Adressevælger (no key), buildings and units from the BBR register on Datafordeler's GraphQL service
(DATAFORDELER_API_KEY). Every BBR query asks for the data as registered and valid now.

`datafordeler-mcp --self-test` checks the server offline: the tools are registered and the code lists translate.
"""

import asyncio
import re
import sys
from collections import Counter
from typing import Any, Literal

import httpx
from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import adressevaelger, geo, graphql
from .codes import FIELD_LISTS, code_lists, translate

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
# What lists of buildings (an area, a list of addresses) return per building: enough to answer, small enough for many.
COMPACT_BUILDING_FIELDS = (
    "id_lokalId status husnummer byg021BygningensAnvendelse byg026Opfoerelsesaar byg027OmTilbygningsaar "
    "byg038SamletBygningsareal byg039BygningensSamledeBoligAreal byg040BygningensSamledeErhvervsAreal "
    "byg054AntalEtager byg056Varmeinstallation byg057Opvarmningsmiddel byg058SupplerendeVarme byg404Koordinat { wkt }"
)
UNIT_FIELDS = (
    "id_lokalId status kommunekode bygning adresseIdentificerer etage opgang enh020EnhedensAnvendelse enh023Boligtype "
    "enh026EnhedensSamledeAreal enh027ArealTilBeboelse enh028ArealTilErhverv enh031AntalVaerelser enh032Toiletforhold "
    "enh033Badeforhold enh034Koekkenforhold enh035Energiforsyning enh045Udlejningsforhold enh051Varmeinstallation "
    "enh052Opvarmningsmiddel enh053SupplerendeVarme enh065AntalVandskylledeToiletter enh066AntalBadevaerelser"
)
COMPACT_UNIT_FIELDS = (
    "id_lokalId status bygning adresseIdentificerer enh020EnhedensAnvendelse enh023Boligtype "
    "enh026EnhedensSamledeAreal enh031AntalVaerelser enh051Varmeinstallation enh052Opvarmningsmiddel "
    "enh053SupplerendeVarme"
)

# An area returns every building in it, a list of addresses every building at them: guards against answers too large
# to be useful (narrow the area or the filters instead).
MAX_AREA_BUILDINGS = 10_000
MAX_ADDRESSES = 200
SEARCH_CONCURRENCY = 10

UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
POINT = re.compile(r"POINT\s*\(\s*([-\d.]+)\s+([-\d.]+)\s*\)", re.I)
YEAR_BANDS = (
    (0, 1899, "before 1900"),
    (1900, 1949, "1900-1949"),
    (1950, 1979, "1950-1979"),
    (1980, 1999, "1980-1999"),
    (2000, 2014, "2000-2014"),
    (2015, 9999, "2015 or later"),
)


# ------------------------------------------------------------------ helpers


def _one_of(**ids: str | None) -> tuple[str, str]:
    given = {name: value.strip() for name, value in ids.items() if value and value.strip()}
    if len(given) != 1:
        raise ToolError(f"Give exactly one of: {', '.join(ids)}")
    name, value = next(iter(given.items()))
    _check_id(name, value)
    return name, value


def _check_id(name: str, value: str) -> None:
    if name == "jordstykke_id":
        if not value.isdigit():
            raise ToolError("jordstykke_id is the numeric id of the parcel (MAT jordstykke), e.g. 1234567")
    elif not UUID.match(value):
        raise ToolError(f"{name} must be a UUID, got {value[:60]!r}")


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


def _split(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Standing rows, the rest with their state, and how many were registered in error (raw BBR rows in)."""
    standing, not_existing, in_error = [], [], 0
    for row in rows:
        status = str(row.get("status"))
        if status == STANDING:
            standing.append(row)
        elif status == REGISTERED_IN_ERROR:
            in_error += 1
        else:
            not_existing.append({"state": STATES.get(status, "unknown status"), **row})
    return standing, not_existing, in_error


def _grouped(result: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """`result` with `count`/`results` (standing) and, when there are any, `not_existing` and the error count."""
    standing, not_existing, in_error = _split(rows)
    result |= {"count": len(standing), "results": [_row(r) for r in standing]}
    if not_existing:
        result |= {
            "not_existing_count": len(not_existing),
            "not_existing": [_row(r) for r in not_existing],
            "note": NOT_EXISTING_NOTE,
        }
    if in_error:
        result["registered_in_error_count"] = in_error
    return result


def _counts(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    counter = Counter(str(r[field]) if r.get(field) is not None else None for r in rows)
    out = []
    for code, n in counter.most_common():
        if code is None:
            out.append({"code": None, "text": "not registered", "count": n})
        else:
            out.append({**translate(field, code), "count": n})
    return out


def _summary(buildings: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts over standing buildings (raw BBR rows): what an area or a list of customers is made of."""
    years = Counter()
    for b in buildings:
        year = b.get("byg026Opfoerelsesaar")
        years[next((label for lo, hi, label in YEAR_BANDS if lo <= year <= hi), "unknown") if year else "unknown"] += 1
    total = sum(
        b.get("byg038SamletBygningsareal") or 0 for b in buildings if (b.get("byg038SamletBygningsareal") or 0) > 0
    )
    dwelling = sum(
        b.get("byg039BygningensSamledeBoligAreal") or 0
        for b in buildings
        if (b.get("byg039BygningensSamledeBoligAreal") or 0) > 0
    )
    return {
        "buildings": len(buildings),
        "by_use": _counts(buildings, "byg021BygningensAnvendelse"),
        "by_heating_installation": _counts(buildings, "byg056Varmeinstallation"),
        "by_heating_medium": _counts(buildings, "byg057Opvarmningsmiddel"),
        "by_supplementary_heat": _counts(buildings, "byg058SupplerendeVarme"),
        "by_year_built": [
            {"years": label, "count": years[label]}
            for label in [b[2] for b in YEAR_BANDS] + ["unknown"]
            if years[label]
        ],
        "total_building_area_m2": total,
        "total_dwelling_area_m2": dwelling,
        "note": "Heating code 99 means the heating is registered on the units (bbr_units), common in apartment blocks.",
    }


def _codes(field: str, values: list[str] | None) -> list[str] | None:
    """Codes for a filter: codes as given, or their exact text (any case), checked against the field's code list."""
    if not values:
        return None
    codes = code_lists()[FIELD_LISTS[field]]
    by_text = {text.lower(): code for code, text in codes.items()}
    out = []
    for value in values:
        value = str(value).strip()
        if value in codes:
            out.append(value)
        elif value.lower() in by_text:
            out.append(by_text[value.lower()])
        else:
            near = [f"{c} {t}" for c, t in codes.items() if value.lower() in t.lower()][:8]
            raise ToolError(
                f"{value!r} is not a code of {field}" + (f"; did you mean: {'; '.join(near)}" if near else "")
            )
    return out


async def _bbr_rows(
    client: httpx.AsyncClient, entity: str, where: dict[str, Any], fields: str, limit: int
) -> tuple[list[dict[str, Any]], bool]:
    try:
        return await graphql.query(client, BBR, entity, where, fields, limit)
    except graphql.DatafordelerError as e:
        raise ToolError(str(e)) from None


async def _bbr_in(
    client: httpx.AsyncClient, entity: str, field: str, ids: list[str], fields: str
) -> list[dict[str, Any]]:
    """Every row whose `field` is one of `ids`, in batches of Datafordeler's `in` limit, at once."""
    batches = [ids[i : i + graphql.MAX_IN] for i in range(0, len(ids), graphql.MAX_IN)]
    pages = await asyncio.gather(
        *[_bbr_rows(client, entity, {field: {"in": b}}, fields, 10 * graphql.PAGE_SIZE) for b in batches]
    )
    return [row for rows, _ in pages for row in rows]


async def _bbr(entity: str, where: dict[str, Any], fields: str, limit: int) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        rows, truncated = await _bbr_rows(client, entity, where, fields, limit)
    result = _grouped({}, rows)
    result["source"] = BBR_SOURCE
    if truncated:
        result["truncated"] = f"more than {limit} matched; raise limit to see more"
    return result


async def _address_record(client: httpx.AsyncClient, level: str, id: str) -> dict[str, Any] | None:
    try:
        return await adressevaelger.get(client, level, id)
    except adressevaelger.AdressevaelgerError as e:
        raise ToolError(str(e)) from None


async def _husnummer_of_address(address_id: str) -> str:
    async with httpx.AsyncClient() as client:
        address = await _address_record(client, "adresse", address_id)
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


def _normal(text: str) -> str:
    return re.sub(r"[^0-9a-zæøåäöü]", "", text.lower())


async def _match(client: httpx.AsyncClient, text: str, gate: asyncio.Semaphore) -> dict[str, Any]:
    """The best Adressevælger hit for one free-text address, and whether it is the same address written differently."""
    async with gate:
        try:
            hits = await adressevaelger.search(client, text, "adresse", 5)
        except adressevaelger.AdressevaelgerError as e:
            return {"input": text, "status": "error", "error": str(e)}
    usable = [h for h in hits if h.get("type") in ("adresse", "husnummer") and h.get("id")]
    if not usable:
        return {
            "input": text,
            "status": "vague" if hits else "not_found",
            **({"hint": "matches only roads: add the house number and postcode"} if hits else {}),
        }
    top = usable[0]
    match = {
        "titel": top.get("titel"),
        "level": top["type"],
        "adresse_id": top["id"] if top["type"] == "adresse" else None,
        "husnummer_id": top.get("husnummerId") if top["type"] == "adresse" else top["id"],
        "exact": _normal(top.get("titel", "")) == _normal(text),
    }
    if not match["exact"] and len(usable) > 1:
        match["alternatives"] = [h.get("titel") for h in usable[1:4]]
    return {"input": text, "status": "matched", "match": {k: v for k, v in match.items() if v is not None}}


# ------------------------------------------------------------------ tools


@mcp.tool()
async def address_search(
    text: str, level: Literal["adresse", "husnummer"] = "adresse", limit: int = 10
) -> dict[str, Any]:
    """Find Danish addresses from free text, e.g. "Jagtvej 120, 2200" (spelling-tolerant; include the postcode).

    level "adresse" finds unit addresses (with floor and door), "husnummer" finds access addresses (one per entrance,
    what buildings are registered on). Each hit has the DAR id to pass to address_get, bbr_buildings or bbr_units.
    When the text is too vague the hits are roads (type "vejnavn" or "navngivenvejpostnummer"): add a house number.
    For many addresses at once use bbr_buildings_for_addresses.
    """
    if not text.strip():
        raise ToolError("text is empty")
    async with httpx.AsyncClient() as client:
        try:
            hits = await adressevaelger.search(client, text.strip(), level, max(1, min(limit, 100)))
        except adressevaelger.AdressevaelgerError as e:
            raise ToolError(str(e)) from None
    results = [
        {
            k: v
            for k, v in {
                "type": h.get("type"),
                "id": h.get("id"),
                "titel": h.get("titel"),
                "husnummer_id": h.get("husnummerId"),
            }.items()
            if v
        }
        for h in hits[:limit]
    ]
    return {"count": len(results), "results": results, "source": DAR_SOURCE}


@mcp.tool()
async def address_get(id: str) -> dict[str, Any]:
    """Look up one DAR address by id (a unit address or a husnummer/access address): the full address, postcode,
    municipality and road code, the husnummer id, and the entrance's coordinates (EPSG:25832, metres)."""
    _one_of(id=id)
    async with httpx.AsyncClient() as client:
        for level in ("adresse", "husnummer"):
            record = await _address_record(client, level, id.strip())
            if record is not None:
                return _address(level, record)
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
    For many addresses use bbr_buildings_for_addresses, for an area bbr_buildings_in_area.
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


@mcp.tool()
async def bbr_buildings_in_area(
    bbox: list[float] | None = None,
    polygon_wkt: str | None = None,
    center: list[float] | None = None,
    around_address_id: str | None = None,
    radius_m: float | None = None,
    crs: Literal["25832", "4326"] = "25832",
    uses: list[str] | None = None,
    heating: list[str] | None = None,
    heating_medium: list[str] | None = None,
    include_not_existing: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """The BBR buildings in an area, with a summary of what they are: counts by use, heating installation, heating
    medium, supplementary heat and year built, and the total floor and dwelling area.

    The area is exactly one of:
    - bbox: [min_x, min_y, max_x, max_y]
    - polygon_wkt: "POLYGON((x y, x y, ...))"
    - center: [x, y] with radius_m (at most 2500)
    - around_address_id (a DAR address or husnummer id) with radius_m
    Coordinates are EPSG:25832 metres (crs "25832", BBR's own) or WGS84 longitude, latitude (crs "4326", e.g. a
    geometry from other data). At most 25 km² per call.

    Optional filters, each a list of codes or their exact texts (any match counts):
    - uses (byg021): 120 Fritliggende enfamiliehus, 121/122 other detached houses, 131 Række-, kæde- og klyngehus,
      132 Dobbelthus, 140 Etagebolig, 150 Kollegium, 510 Sommerhus, 910 Garage, 920 Carport, 930 Udhus, ...
    - heating (byg056): 1 Fjernvarme/blokvarme, 2 Centralvarme med én fyringsenhed, 3 Ovn til fast og flydende
      brændsel, 5 Varmepumpe, 6 Centralvarme med to fyringsenheder, 7 Elvarme, 8 Gasradiator, 9 Ingen
      varmeinstallation, 99 registered on the units
    - heating_medium (byg057): 1 Elektricitet, 2 Gasværksgas, 3 Flydende brændsel (oil), 4 Fast brændsel, 6 Halm,
      7 Naturgas, 9 Andet

    At most 10,000 buildings per area before the heating filters (BBR cannot filter on heating itself): in a large
    area, filter on uses too. Buildings whose heating is registered on their units (code 99, apartment blocks) do not
    match a heating filter; look at their units with bbr_units.

    By default only standing buildings (status Opført) are counted and listed; include_not_existing adds the planned,
    demolished and shelved ones apart, in not_existing. The summary covers every matching building; results lists at
    most `limit` of them (compact fields), so ask for the list only when it is needed.
    """
    try:
        given = [
            name
            for name, v in (
                ("bbox", bbox),
                ("polygon_wkt", polygon_wkt),
                ("center", center),
                ("around_address_id", around_address_id),
            )
            if v
        ]
        if len(given) != 1:
            raise ToolError(
                "Give exactly one area: bbox, polygon_wkt, center (with radius_m) or around_address_id (with radius_m)"
            )
        if given[0] in ("center", "around_address_id") and not radius_m:
            raise ToolError(f"{given[0]} needs radius_m")
        if bbox:
            area_wkt, km2 = geo.bbox(bbox, crs)
        elif polygon_wkt:
            area_wkt, km2 = geo.polygon(polygon_wkt, crs)
        elif center:
            if len(center) != 2:
                raise ToolError("center is [x, y] (or [lon, lat] with crs 4326)")
            area_wkt, km2 = geo.circle(center[0], center[1], radius_m or 0, crs)
        else:
            _check_id("around_address_id", around_address_id or "")
            async with httpx.AsyncClient() as client:
                found = None
                for level in ("adresse", "husnummer"):
                    if (record := await _address_record(client, level, (around_address_id or "").strip())) is not None:
                        found = _address(level, record)
                        break
            if not found or not found.get("koordinater"):
                raise ToolError(f"No DAR address with coordinates for id {around_address_id}")
            area_wkt, km2 = geo.circle(found["koordinater"]["x"], found["koordinater"]["y"], radius_m or 0)
    except geo.AreaError as e:
        raise ToolError(str(e)) from None

    # BBR filters on use and status itself; its filter has no heating fields, so those are applied to what comes back.
    filters: list[dict[str, Any]] = [{"byg404Koordinat": {"intersects": {"wkt": area_wkt, "crs": 25832}}}]
    if use_codes := _codes("byg021BygningensAnvendelse", uses):
        filters.append({"byg021BygningensAnvendelse": {"in": use_codes}})
    if not include_not_existing:
        filters.append({"status": {"eq": STANDING}})
    after = {
        field: set(codes)
        for field, values in (("byg056Varmeinstallation", heating), ("byg057Opvarmningsmiddel", heating_medium))
        if (codes := _codes(field, values))
    }
    async with httpx.AsyncClient() as client:
        rows, more = await _bbr_rows(
            client, "BBR_Bygning", {"and": filters}, COMPACT_BUILDING_FIELDS, MAX_AREA_BUILDINGS
        )
    if more:
        raise ToolError(
            f"More than {MAX_AREA_BUILDINGS} buildings in the area"
            + (" of those uses" if use_codes else "")
            + ": use a smaller area or filter on uses (heating filters apply only after the buildings are fetched)"
        )
    rows = [r for r in rows if all(str(r.get(field)) in codes for field, codes in after.items())]
    standing, not_existing, in_error = _split(rows)
    limit = max(0, min(limit, 1000))
    result: dict[str, Any] = {
        "area": {"km2": round(km2, 3), "wkt_epsg25832": area_wkt},
        "summary": _summary(standing),
        "count": len(standing),
        "results": [_row(b) for b in standing[:limit]],
    }
    if len(standing) > limit:
        result["results_truncated"] = f"{len(standing)} buildings, {limit} listed; the summary covers all"
    if not_existing:
        result |= {
            "not_existing_count": len(not_existing),
            "not_existing_by_state": dict(Counter(b["state"] for b in not_existing)),
            "not_existing": [_row(b) for b in not_existing[:limit]],
            "note": NOT_EXISTING_NOTE,
        }
    if in_error:
        result["registered_in_error_count"] = in_error
    result["source"] = BBR_SOURCE
    return result


@mcp.tool()
async def bbr_buildings_for_addresses(
    addresses: list[str] | None = None,
    husnummer_ids: list[str] | None = None,
    include_units: bool = False,
) -> dict[str, Any]:
    """BBR buildings for a list of addresses at once (e.g. a list of customers), with a summary over all of them.

    Give addresses as free text (e.g. "Jagtvej 120, 1. tv, 2200 København N"; include house number and postcode) or,
    when you already have them, husnummer_ids (DAR access address ids). At most 200 per call.

    Each free-text address is matched with Adressevælger: status "matched" with the matched address (`titel`) and
    whether it is the same address written differently (`exact`); otherwise "not_found" or "vague" (only roads
    matched). Check non-exact matches against the input before relying on them. Buildings are looked up on the
    matched address's husnummer, so every building at that entrance is returned, outbuildings included. With
    include_units, the units of addresses matched with floor/door are added (their own heating, enh051-053).

    The summary counts the standing buildings of all matched addresses (by use, heating installation, heating medium,
    supplementary heat, year built). Each address lists its standing buildings and, apart, those not existing.
    """
    addresses = [a.strip() for a in addresses or [] if a and a.strip()]
    husnummer_ids = [h.strip() for h in husnummer_ids or [] if h and h.strip()]
    if bool(addresses) == bool(husnummer_ids):
        raise ToolError("Give either addresses or husnummer_ids")
    if len(addresses) + len(husnummer_ids) > MAX_ADDRESSES:
        raise ToolError(f"At most {MAX_ADDRESSES} per call: split the list")
    for h in husnummer_ids:
        _check_id("husnummer_ids", h)

    async with httpx.AsyncClient() as client:
        if addresses:
            gate = asyncio.Semaphore(SEARCH_CONCURRENCY)
            entries = await asyncio.gather(*[_match(client, a, gate) for a in addresses])
        else:
            entries = [{"input": h, "status": "matched", "match": {"husnummer_id": h}} for h in husnummer_ids]
        houses = sorted({e["match"]["husnummer_id"] for e in entries if e["status"] == "matched"})
        buildings = await _bbr_in(client, "BBR_Bygning", "husnummer", houses, COMPACT_BUILDING_FIELDS) if houses else []
        unit_ids = (
            sorted(
                {e["match"]["adresse_id"] for e in entries if e["status"] == "matched" and e["match"].get("adresse_id")}
            )
            if include_units
            else []
        )
        units = (
            await _bbr_in(client, "BBR_Enhed", "adresseIdentificerer", unit_ids, COMPACT_UNIT_FIELDS)
            if unit_ids
            else []
        )

    by_house: dict[str, list[dict[str, Any]]] = {}
    for b in buildings:
        by_house.setdefault(b.get("husnummer"), []).append(b)
    by_address: dict[str, list[dict[str, Any]]] = {}
    for u in units:
        by_address.setdefault(u.get("adresseIdentificerer"), []).append(u)

    standing_all = []
    for entry in entries:
        if entry["status"] != "matched":
            continue
        here = by_house.get(entry["match"]["husnummer_id"], [])
        standing_all.extend(b for b in here if str(b.get("status")) == STANDING)
        _grouped(entry, here)
        entry["buildings"], entry["building_count"] = entry.pop("results"), entry.pop("count")
        entry.pop("note", None)
        if include_units and entry["match"].get("adresse_id"):
            unit_rows = by_address.get(entry["match"]["adresse_id"], [])
            entry["units"] = [_row(u) for u in _split(unit_rows)[0]]

    # A building at several of the listed addresses (one entrance, many flats) is counted once.
    unique = list({b["id_lokalId"]: b for b in standing_all}.values())
    statuses = Counter(e["status"] for e in entries)
    return {
        "summary": {
            "addresses": len(entries),
            "matched": statuses["matched"],
            "matched_exactly": sum(1 for e in entries if e.get("match", {}).get("exact")),
            "not_found": statuses["not_found"],
            "vague": statuses["vague"],
            **({"errors": statuses["error"]} if statuses["error"] else {}),
            **_summary(unique),
        },
        "results": entries,
        "note": NOT_EXISTING_NOTE,
        "source": f"{BBR_SOURCE} {DAR_SOURCE}",
    }


async def _self_test() -> None:
    async with Client(mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    expected = {
        "address_search",
        "address_get",
        "bbr_buildings",
        "bbr_units",
        "bbr_buildings_in_area",
        "bbr_buildings_for_addresses",
    }
    if names != expected:
        sys.exit(f"self-test failed: tools {sorted(names)}, expected {sorted(expected)}")
    heating = translate("byg056Varmeinstallation", "1")
    if heating != {"code": "1", "text": "Fjernvarme/blokvarme"}:
        sys.exit(f"self-test failed: byg056 code 1 translated to {heating}")
    x, y = geo.wgs84_to_utm32(9.0, 0.0)
    if abs(x - 500000) > 0.001 or abs(y) > 0.001:
        sys.exit(f"self-test failed: the projection puts 9°E 0°N at {x}, {y}")
    print(f"self-test ok: {len(names)} tools, {len(code_lists())} code lists", file=sys.stderr)


def main() -> None:
    if sys.argv[1:] == ["--self-test"]:
        asyncio.run(_self_test())
        return
    mcp.run()


if __name__ == "__main__":
    main()
