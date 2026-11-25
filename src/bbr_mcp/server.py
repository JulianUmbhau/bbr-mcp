"""MCP server for Danish buildings (BBR) and addresses (DAR).

Addresses come from Adressevælger (no key), buildings and units from the BBR register on Datafordeler's GraphQL service
(DATAFORDELER_API_KEY). Every BBR query asks for the data as registered and valid now.

`bbr-mcp --self-test` checks the server offline: the tools are registered and the code lists translate.
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

mcp = MCPServer("bbr")

BBR = "BBR/v3"
BBR_SOURCE = (
    "Bygnings- og Boligregistret (BBR), via Datafordeler, CC BY 4.0. "
    "Grouping by status and the state labels are added by bbr-mcp."
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

TECH_FIELDS = (
    "id_lokalId status tek020Klassifikation tek024Etableringsaar tek039Effekt tek032Stoerrelse tek110Driftstatus "
    "bygning grund husnummer jordstykke tek109Koordinat { wkt }"
)
POWER_NOTE = (
    "power is tek039Effekt as registered, meant in kW, but not consistent: some are registered in W (a rooftop solar "
    "plant of 71100 next to others of 6). Check the largest before using a total; many have no power registered."
)
OPGANG_FIELDS = "adgangFraHusnummer bygning status"
PROPERTY_FIELDS = "id_lokalId bfeNummer samletFastEjendom ejerlejlighed bygningPaaFremmedGrund"
PROPERTY_KINDS = (
    ("samletFastEjendom", "samlet fast ejendom (land and everything on it)"),
    ("ejerlejlighed", "ejerlejlighed (an owner flat or unit)"),
    ("bygningPaaFremmedGrund", "bygning på fremmed grund (a building on someone else's land)"),
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
        if field in ("byg404Koordinat", "tek109Koordinat"):
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
        "note": "Heating code 99 means the heating is registered on the units (units), common in blocks of flats.",
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


async def _by_ids(client: httpx.AsyncClient, ids: set[str], fields: str) -> dict[str, dict[str, Any]]:
    rows = await _bbr_in(client, "BBR_Bygning", "id_lokalId", sorted(ids), fields) if ids else []
    return {r["id_lokalId"]: r for r in rows}


def _ordered(by_id: dict[str, dict[str, Any]], ids: set[str]) -> list[dict[str, Any]]:
    return [by_id[i] for i in sorted(ids) if i in by_id]


async def _buildings_of_houses(
    client: httpx.AsyncClient, houses: list[str], fields: str
) -> dict[str, tuple[list[dict[str, Any]], str]]:
    """Each husnummer's buildings, and how they were found.

    A building names one representative husnummer; its other entrances are linked through its stairwells (BBR
    Opgang.adgangFraHusnummer). So a husnummer with no building of its own is looked up as an entrance.
    """
    out: dict[str, tuple[list[dict[str, Any]], str]] = {h: ([], "none") for h in houses}
    for row in await _bbr_in(client, "BBR_Bygning", "husnummer", houses, fields) if houses else []:
        if row.get("husnummer") in out:
            out[row["husnummer"]][0].append(row)
    out = {h: (rows, "husnummer" if rows else "none") for h, (rows, _) in out.items()}
    missing = [h for h, (rows, _) in out.items() if not rows]
    if missing:
        wanted: dict[str, set[str]] = {}
        for stair in await _bbr_in(client, "BBR_Opgang", "adgangFraHusnummer", missing, OPGANG_FIELDS):
            if stair.get("bygning"):
                wanted.setdefault(stair["adgangFraHusnummer"], set()).add(stair["bygning"])
        by_id = await _by_ids(client, {b for ids in wanted.values() for b in ids}, fields)
        for house, ids in wanted.items():
            if rows := _ordered(by_id, ids):
                out[house] = (rows, "entrance (opgang)")
    return out


async def _buildings_of_addresses(
    client: httpx.AsyncClient, addresses: dict[str, str | None], fields: str, unit_fields: str
) -> dict[str, tuple[list[dict[str, Any]], str, list[dict[str, Any]]]]:
    """Each unit address's buildings, how they were found, and its units.

    A unit address (a flat, a shop) belongs to a BBR unit, which names its building: the exact building, also when
    the entrance is not the building's representative husnummer. An address with no unit falls back to its husnummer
    (the value in `addresses`), and from there to the entrances.
    """
    ids = sorted(addresses)
    units = await _bbr_in(client, "BBR_Enhed", "adresseIdentificerer", ids, unit_fields) if ids else []
    units_of: dict[str, list[dict[str, Any]]] = {}
    for unit in units:
        units_of.setdefault(unit["adresseIdentificerer"], []).append(unit)

    def homes(address: str) -> set[str]:
        here = units_of.get(address, [])
        standing = [u for u in here if str(u.get("status")) == STANDING] or here
        return {u["bygning"] for u in standing if u.get("bygning")}

    by_id = await _by_ids(client, {b for a in ids for b in homes(a)}, fields)
    out: dict[str, tuple[list[dict[str, Any]], str, list[dict[str, Any]]]] = {}
    fallback: dict[str, str] = {}
    for address in ids:
        if rows := _ordered(by_id, homes(address)):
            out[address] = (rows, "unit", units_of.get(address, []))
        elif addresses[address]:
            fallback[address] = addresses[address]  # type: ignore[assignment]
        else:
            out[address] = ([], "none", units_of.get(address, []))
    if fallback:
        houses = await _buildings_of_houses(client, sorted(set(fallback.values())), fields)
        for address, house in fallback.items():
            rows, via = houses[house]
            out[address] = (rows, via, units_of.get(address, []))
    return out


async def _properties(
    client: httpx.AsyncClient, bfes: list[int], fields: str, unit_fields: str
) -> dict[int, dict[str, Any]]:
    """Each BFE number's kind, buildings, grunde and (for an owner flat) units.

    BBR links a property (Ejendomsrelation, by bfeNummer) to its land (Grund.bestemtFastEjendom, and the buildings on
    those grunde), to a building on someone else's land (BygningEjendomsrelation), or to the units of an owner flat
    (EnhedEjendomsrelation, and the buildings they are in).
    """
    relations = await _bbr_in(client, "BBR_Ejendomsrelation", "bfeNummer", bfes, PROPERTY_FIELDS)  # type: ignore[arg-type]
    bfe_of = {r["id_lokalId"]: r["bfeNummer"] for r in relations}
    out: dict[int, dict[str, Any]] = {b: {"found": False} for b in bfes}
    for r in relations:
        kind = next((text for field, text in PROPERTY_KINDS if r.get(field)), "unknown")
        out[r["bfeNummer"]] = {"found": True, "kind": kind, "grunde": set(), "building_ids": set(), "units": []}
    rel_ids = sorted(bfe_of)
    if not rel_ids:
        return out
    grunde, foreign, flat_units, flat_buildings = await asyncio.gather(
        _bbr_in(client, "BBR_Grund", "bestemtFastEjendom", rel_ids, "id_lokalId bestemtFastEjendom"),
        _bbr_in(
            client, "BBR_BygningEjendomsrelation", "bygningPaaFremmedGrund", rel_ids, "bygning bygningPaaFremmedGrund"
        ),
        _bbr_in(client, "BBR_EnhedEjendomsrelation", "ejerlejlighed", rel_ids, "enhed ejerlejlighed"),
        _bbr_in(client, "BBR_Bygning", "ejerlejlighed", rel_ids, "id_lokalId ejerlejlighed"),
    )
    for g in grunde:
        out[bfe_of[g["bestemtFastEjendom"]]]["grunde"].add(g["id_lokalId"])
    for link in foreign:
        if link.get("bygning"):
            out[bfe_of[link["bygningPaaFremmedGrund"]]]["building_ids"].add(link["bygning"])
    for b in flat_buildings:
        out[bfe_of[b["ejerlejlighed"]]]["building_ids"].add(b["id_lokalId"])
    grund_of = {g: bfe for bfe, p in out.items() if p["found"] for g in p["grunde"]}
    unit_ids = sorted({link["enhed"] for link in flat_units if link.get("enhed")})
    on_land, units = await asyncio.gather(
        _bbr_in(client, "BBR_Bygning", "grund", sorted(grund_of), "id_lokalId grund")
        if grund_of
        else asyncio.sleep(0, []),
        _bbr_in(client, "BBR_Enhed", "id_lokalId", unit_ids, unit_fields) if unit_ids else asyncio.sleep(0, []),
    )
    for b in on_land:
        out[grund_of[b["grund"]]]["building_ids"].add(b["id_lokalId"])
    flat_of = {link["enhed"]: bfe_of[link["ejerlejlighed"]] for link in flat_units if link.get("enhed")}
    for unit in units:
        bfe = flat_of[unit["id_lokalId"]]
        out[bfe]["units"].append(unit)
        if unit.get("bygning"):
            out[bfe]["building_ids"].add(unit["bygning"])
    by_id = await _by_ids(client, {b for p in out.values() if p["found"] for b in p["building_ids"]}, fields)
    for p in out.values():
        if p["found"]:
            p["buildings"] = _ordered(by_id, p["building_ids"])
    return out


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


def _normal(text: str) -> list[str]:
    """The address as its words and numbers, in order: "1, 11. tv" and "11, 1. tv" differ, "12A" equals "12 A"."""
    return re.findall(r"\d+|[^\W\d_]+", text.lower())


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


async def _area(
    bbox: list[float] | None,
    polygon_wkt: str | None,
    center: list[float] | None,
    around_address_id: str | None,
    radius_m: float | None,
    crs: str,
) -> tuple[str, float]:
    """The area as a WKT polygon in EPSG:25832 and its size in km²."""
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
    return area_wkt, km2


# ------------------------------------------------------------------ tools


@mcp.tool()
async def address_search(
    text: str, level: Literal["adresse", "husnummer"] = "adresse", limit: int = 10
) -> dict[str, Any]:
    """Find Danish addresses from free text, e.g. "Jagtvej 120, 2200" (spelling-tolerant; include the postcode).

    level "adresse" finds unit addresses (with floor and door), "husnummer" finds access addresses (one per entrance,
    what buildings are registered on). Each hit has the DAR id to pass to address_get, buildings or units.
    When the text is too vague the hits are roads (type "vejnavn" or "navngivenvejpostnummer"): add a house number.
    For many addresses at once use buildings_for_addresses.
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
async def buildings(
    husnummer_id: str | None = None,
    address_id: str | None = None,
    building_id: str | None = None,
    jordstykke_id: str | None = None,
    bfe_nummer: int | None = None,
    include_units: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """BBR buildings, by exactly one of:
    - address_id (DAR unit address, e.g. a flat): the building of its BBR unit, the exact one; an address without a
      unit falls back to its husnummer
    - husnummer_id (DAR access address): the buildings registered on it, or, when it is another entrance to a
      building (a block with several stairwells), that building
    - building_id (BBR bygning id), jordstykke_id (cadastral parcel)
    - bfe_nummer (a property): every building on it. For an owner flat (ejerlejlighed) the BFE is the flat, so its
      units and their building are returned; a rented or cooperative flat has no BFE of its own.
    found_via says how they were found. With include_units the units are added: those at the address for
    address_id, those of the owner flat for its BFE, otherwise all units in the buildings (at most `limit`).

    Returns use (byg021), year built and rebuilt, areas, floors, materials, heating installation (byg056), heating
    medium (byg057), supplementary heat (byg058), listing status and the coordinates (EPSG:25832). Coded fields come
    as {"code", "text"}; empty fields are left out. Outbuildings (garages, sheds) are buildings too.

    results and count are the buildings standing now. Planned, under-construction, partly completed, demolished and
    shelved buildings are listed apart in not_existing, each with its state; never count those as existing buildings.
    For many addresses use buildings_for_addresses, for an area buildings_in_area, for solar, wind and other
    installations technical_installations.
    """
    limit = max(1, min(limit, 1000))
    ids = {
        "husnummer_id": husnummer_id,
        "address_id": address_id,
        "building_id": building_id,
        "jordstykke_id": jordstykke_id,
    }
    if bfe_nummer is not None:
        if any(v and v.strip() for v in ids.values()):
            raise ToolError(f"Give exactly one of: {', '.join(ids)}, bfe_nummer")
        if bfe_nummer <= 0:
            raise ToolError("bfe_nummer is a positive number")
        name, value = "bfe_nummer", str(bfe_nummer)
    else:
        name, value = _one_of(**ids)

    extra: dict[str, Any] = {}
    units: list[dict[str, Any]] | None = None
    async with httpx.AsyncClient() as client:
        if name in ("building_id", "jordstykke_id"):
            field = {"building_id": "id_lokalId", "jordstykke_id": "jordstykke"}[name]
            rows, truncated = await _bbr_rows(client, "BBR_Bygning", {field: {"eq": value}}, BUILDING_FIELDS, limit)
            via = name.removesuffix("_id")
        elif name == "husnummer_id":
            rows, via = (await _buildings_of_houses(client, [value], BUILDING_FIELDS))[value]
            truncated = False
        elif name == "address_id":
            address = await _address_record(client, "adresse", value)
            if address is None:
                raise ToolError(f"No DAR address {value}; if it is a husnummer (adgangsadresse) id, pass husnummer_id")
            house = address["husnummer"]["id_lokalid"]
            rows, via, units = (await _buildings_of_addresses(client, {value: house}, BUILDING_FIELDS, UNIT_FIELDS))[
                value
            ]
            extra["husnummer_id"] = house
            truncated = False
        else:
            found = (await _properties(client, [bfe_nummer], BUILDING_FIELDS, UNIT_FIELDS))[bfe_nummer]  # type: ignore[index]
            if not found["found"]:
                raise ToolError(f"No property with BFE number {bfe_nummer} in BBR")
            rows, via, truncated = found["buildings"], "bfe", False
            extra["property"] = {"bfe_nummer": bfe_nummer, "kind": found["kind"], "grunde": len(found["grunde"])}
            if found["units"]:
                units = found["units"]
        if include_units and units is None:
            ids_ = sorted({r["id_lokalId"] for r in rows})
            units = await _bbr_in(client, "BBR_Enhed", "bygning", ids_, UNIT_FIELDS) if ids_ else []
    result = _grouped({"found_via": via, **extra}, rows[:limit])
    if include_units:
        unit_rows = _split(units or [])[0]
        result["units"] = [_row(u) for u in unit_rows[:limit]]
        if len(unit_rows) > limit:
            result["units_truncated"] = f"{len(unit_rows)} units, {limit} listed"
    if truncated or len(rows) > limit:
        result["truncated"] = f"more than {limit} matched; raise limit to see more"
    if via == "none":
        result["note_found"] = "No building is registered on this address or entrance in BBR."
    result["source"] = BBR_SOURCE
    return result


@mcp.tool()
async def units(
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
async def buildings_in_area(
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
    area, filter on uses too. Buildings whose heating is registered on their units (code 99, blocks of flats) match
    only a filter on 99, never one on codes 1-9; look at their units with units.

    By default only standing buildings (status Opført) are counted and listed; include_not_existing adds the planned,
    demolished and shelved ones apart, in not_existing. The summary covers every matching building; results lists at
    most `limit` of them (compact fields), so ask for the list only when it is needed.
    """
    area_wkt, km2 = await _area(bbox, polygon_wkt, center, around_address_id, radius_m, crs)

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
async def technical_installations(
    bfe_nummer: int | None = None,
    husnummer_id: str | None = None,
    building_id: str | None = None,
    jordstykke_id: str | None = None,
    bbox: list[float] | None = None,
    polygon_wkt: str | None = None,
    center: list[float] | None = None,
    around_address_id: str | None = None,
    radius_m: float | None = None,
    crs: Literal["25832", "4326"] = "25832",
    types: list[str] | None = None,
    include_not_existing: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """BBR technical installations (tekniske anlæg): solar PV, wind turbines, solar heating, oil and gas burners,
    tanks, transformer stations and more, with a summary by type, count and total registered power.

    Where, exactly one of:
    - bfe_nummer (a property): those on its land and on its buildings (rooftop solar belongs to the building)
    - husnummer_id, building_id or jordstykke_id
    - an area, as in buildings_in_area: bbox, polygon_wkt, center + radius_m or around_address_id + radius_m, in
      crs 25832 or 4326 (at most 25 km²). Only installations with their own coordinates are found by area.

    types filters on the classification (tek020), codes or exact texts: 1230 Solcelleanlæg (solar PV),
    1210 Vindmølle, 1235 Solvarmeanlæg (solar heating), 1280 Biogasanlæg, 1240 Nødstrømsforsyningsanlæg,
    1250 Transformerstation, 1255 Oliefyr, 1265 Naturgasfyr, 1275 Halmfyr, 1285 Pillefyr, 1290 Brændeovn,
    1295 Andet energianlæg, 1110 Tank, ...

    Each installation has its type, year established (tek024), registered power (tek039Effekt, meant in kW but
    sometimes registered in W: see the summary's note),
    operating status (tek110) and what it belongs to (building, grund, husnummer, jordstykke). BBR has no code for
    heat pumps or batteries: a heat pump is a building's heating installation (byg056 = 5). BBR's registration of
    small solar systems is not complete, so counts are a lower bound.

    results and count are the installations that exist now; with include_not_existing the removed and planned ones
    are counted apart.
    """
    limit = max(0, min(limit, 1000))
    places = {"husnummer_id": husnummer_id, "building_id": building_id, "jordstykke_id": jordstykke_id}
    area_given = any(v for v in (bbox, polygon_wkt, center, around_address_id))
    given = [n for n, v in places.items() if v and v.strip()] + (["bfe_nummer"] if bfe_nummer is not None else [])
    if len(given) + area_given != 1:
        raise ToolError("Give exactly one of: bfe_nummer, husnummer_id, building_id, jordstykke_id or an area")
    filters: list[dict[str, Any]] = []
    if type_codes := _codes("tek020Klassifikation", types):
        filters.append({"tek020Klassifikation": {"in": type_codes}})
    if not include_not_existing:
        filters.append({"status": {"eq": STANDING}})
    extra: dict[str, Any] = {}

    async with httpx.AsyncClient() as client:
        if area_given:
            area_wkt, km2 = await _area(bbox, polygon_wkt, center, around_address_id, radius_m, crs)
            extra["area"] = {"km2": round(km2, 3), "wkt_epsg25832": area_wkt}
            wheres = [{"tek109Koordinat": {"intersects": {"wkt": area_wkt, "crs": 25832}}}]
        elif bfe_nummer is not None:
            if bfe_nummer <= 0:
                raise ToolError("bfe_nummer is a positive number")
            found = (await _properties(client, [bfe_nummer], "id_lokalId status", "id_lokalId bygning status"))[
                bfe_nummer
            ]
            if not found["found"]:
                raise ToolError(f"No property with BFE number {bfe_nummer} in BBR")
            extra["property"] = {"bfe_nummer": bfe_nummer, "kind": found["kind"]}
            wheres = [
                {field: {"in": ids}}
                for field, ids in (("grund", sorted(found["grunde"])), ("bygning", sorted(found["building_ids"])))
                if ids
            ]
        else:
            name = given[0]
            _check_id(name, places[name].strip())  # type: ignore[union-attr]
            field = {"husnummer_id": "husnummer", "building_id": "bygning", "jordstykke_id": "jordstykke"}[name]
            wheres = [{field: {"eq": places[name].strip()}}]  # type: ignore[union-attr]

        rows: dict[str, dict[str, Any]] = {}
        for where in wheres:
            # `in` lists are at most 100 ids: split them.
            ((field, cond),) = where.items()
            values = cond.get("in")
            parts = (
                [{field: {"in": values[i : i + graphql.MAX_IN]}} for i in range(0, len(values), graphql.MAX_IN)]
                if values
                else [where]
            )
            pages = await asyncio.gather(
                *[
                    _bbr_rows(client, "BBR_TekniskAnlaeg", {"and": [part, *filters]}, TECH_FIELDS, MAX_AREA_BUILDINGS)
                    for part in parts
                ]
            )
            for found_rows, more in pages:
                if more:
                    raise ToolError(
                        f"More than {MAX_AREA_BUILDINGS} installations: use a smaller area or filter on types"
                    )
                rows.update({r["id_lokalId"]: r for r in found_rows})

    standing, not_existing, in_error = _split(list(rows.values()))
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in standing:
        grouped.setdefault(str(r.get("tek020Klassifikation")), []).append(r)
    by_type = {}
    for code, group in grouped.items():
        power = sorted(r["tek039Effekt"] for r in group if r.get("tek039Effekt"))
        entry: dict[str, Any] = {**translate("tek020Klassifikation", code), "count": len(group)}
        if power:
            entry["power"] = {
                "registered": len(power),
                "median": power[len(power) // 2],
                "max": power[-1],
                "total": sum(power),
            }
        by_type[code] = entry
    result: dict[str, Any] = {
        **extra,
        "summary": {
            "installations": len(standing),
            "by_type": sorted(by_type.values(), key=lambda e: -e["count"]),
            "note": POWER_NOTE,
        },
        "count": len(standing),
        "results": [_row(r) for r in standing[:limit]],
    }
    if len(standing) > limit:
        result["results_truncated"] = f"{len(standing)} installations, {limit} listed; the summary covers all"
    if not_existing:
        result["not_existing_count"] = len(not_existing)
        result["not_existing_by_state"] = dict(Counter(r["state"] for r in not_existing))
    if in_error:
        result["registered_in_error_count"] = in_error
    result["source"] = BBR_SOURCE
    return result


@mcp.tool()
async def buildings_for_addresses(
    addresses: list[str] | None = None,
    husnummer_ids: list[str] | None = None,
    building_ids: list[str] | None = None,
    bfe_numbers: list[int] | None = None,
    include_units: bool = False,
    per_address_limit: int = 20,
) -> dict[str, Any]:
    """BBR buildings for a list of addresses, buildings or properties at once (e.g. a list of customers or meters),
    with a summary over all of them.

    Give exactly one of, at most 200 per call:
    - addresses as free text (e.g. "Jagtvej 120, 1. tv, 2200 København N"; include house number and postcode)
    - husnummer_ids (DAR access address ids)
    - building_ids (BBR bygning ids), e.g. from a table that links meters to buildings
    - bfe_numbers (properties): every building on each property; for an owner flat its building
    An id or number not found in BBR has status "not_found".

    Each free-text address is matched with Adressevælger: status "matched" with the matched address (`titel`) and
    whether it is the same address written differently (`exact`); otherwise "not_found" or "vague" (only roads
    matched). Check non-exact matches against the input before relying on them. A matched unit address (a flat, a
    house) gives the building of its BBR unit; a husnummer gives every building at that entrance, outbuildings
    included, or the building it is an entrance to. found_via says which. With include_units the units at each
    address (their own heating, enh051-053), in each building, or of each owner flat are added.

    The summary counts the standing buildings of all entries (by use, heating installation, heating medium,
    supplementary heat, year built) and covers every building; one building in several entries is counted once. Each
    entry lists at most per_address_limit of its standing buildings (compact fields; a large site such as a campus or
    an amusement park has dozens) and counts the ones not existing (not_existing_count). For a long list,
    per_address_limit=0 gives only the matches and the summary.
    """
    addresses = [a.strip() for a in addresses or [] if a and a.strip()]
    husnummer_ids = [h.strip() for h in husnummer_ids or [] if h and h.strip()]
    building_ids = [b.strip() for b in building_ids or [] if b and b.strip()]
    bfes = list(bfe_numbers or [])
    given = [x for x in (addresses, husnummer_ids, building_ids, bfes) if x]
    if len(given) != 1:
        raise ToolError("Give exactly one of addresses, husnummer_ids, building_ids or bfe_numbers")
    if len(given[0]) > MAX_ADDRESSES:
        raise ToolError(f"At most {MAX_ADDRESSES} per call: split the list")
    if per_address_limit < 0:
        raise ToolError("per_address_limit must be 0 or more")
    for h in husnummer_ids:
        _check_id("husnummer_ids", h)
    for b in building_ids:
        _check_id("building_ids", b)
    if any(not isinstance(b, int) or b <= 0 for b in bfes):
        raise ToolError("bfe_numbers are positive numbers")

    async with httpx.AsyncClient() as client:
        if addresses:
            gate = asyncio.Semaphore(SEARCH_CONCURRENCY)
            entries = await asyncio.gather(*[_match(client, a, gate) for a in addresses])
        elif husnummer_ids:
            entries = [{"input": h, "status": "matched", "match": {"husnummer_id": h}} for h in husnummer_ids]
        elif building_ids:
            entries = [{"input": b, "status": "matched", "match": {"building_id": b}} for b in building_ids]
        else:
            entries = [{"input": b, "status": "matched", "match": {"bfe_nummer": b}} for b in bfes]
        found = await _resolve(client, entries, include_units)

    standing_all: list[dict[str, Any]] = []
    for entry in entries:
        if entry["status"] != "matched":
            continue
        key = _key(entry)
        if key not in found:
            entry["status"] = "not_found"
            entry.pop("match", None)
            continue
        rows, via, units, extra = found[key]
        entry["found_via"] = via
        entry.update(extra)
        standing, not_existing, in_error = _split(rows)
        standing_all.extend(standing)
        entry["building_count"] = len(standing)
        entry["buildings"] = [_row(b) for b in standing[:per_address_limit]]
        if len(standing) > per_address_limit:
            entry["buildings_truncated"] = (
                f"{len(standing)} standing, {per_address_limit} listed; the summary covers all, "
                "buildings on this entry lists them"
            )
        if building_ids and not_existing:
            entry["state"] = not_existing[0]["state"]
        if not_existing:
            entry["not_existing_count"] = len(not_existing)
        if in_error:
            entry["registered_in_error_count"] = in_error
        if include_units:
            entry["units"] = [_row(u) for u in _split(units)[0]]

    unique = list({b["id_lokalId"]: b for b in standing_all}.values())
    statuses = Counter(e["status"] for e in entries)
    summary: dict[str, Any] = {"entries": len(entries), "matched": statuses["matched"]}
    if addresses:
        summary["matched_exactly"] = sum(1 for e in entries if e.get("match", {}).get("exact"))
        summary["vague"] = statuses["vague"]
    summary["not_found"] = statuses["not_found"]
    if statuses["error"]:
        summary["errors"] = statuses["error"]
    return {
        "summary": summary | _summary(unique),
        "results": entries,
        "note": NOT_EXISTING_NOTE,
        "source": f"{BBR_SOURCE} {DAR_SOURCE}" if addresses else BBR_SOURCE,
    }


def _key(entry: dict[str, Any]) -> tuple[str, Any]:
    match = entry["match"]
    for name in ("adresse_id", "husnummer_id", "building_id", "bfe_nummer"):
        if match.get(name):
            return name, match[name]
    raise ValueError(entry)


async def _resolve(
    client: httpx.AsyncClient, entries: list[dict[str, Any]], include_units: bool
) -> dict[tuple[str, Any], tuple[list[dict[str, Any]], str, list[dict[str, Any]], dict[str, Any]]]:
    """Each matched entry's buildings, how they were found, its units and extra fields, keyed by `_key`."""
    keys = [_key(e) for e in entries if e["status"] == "matched"]
    out: dict[tuple[str, Any], tuple[list[dict[str, Any]], str, list[dict[str, Any]], dict[str, Any]]] = {}
    by_kind: dict[str, list[Any]] = {}
    for kind, value in keys:
        by_kind.setdefault(kind, []).append(value)

    if addresses := by_kind.get("adresse_id"):
        houses = {
            e["match"]["adresse_id"]: e["match"].get("husnummer_id")
            for e in entries
            if e.get("match", {}).get("adresse_id")
        }
        wanted = {a: houses.get(a) for a in set(addresses)}
        for a, (rows, via, units) in (
            await _buildings_of_addresses(client, wanted, COMPACT_BUILDING_FIELDS, COMPACT_UNIT_FIELDS)
        ).items():
            out["adresse_id", a] = (rows, via, units, {})
    if houses_ := by_kind.get("husnummer_id"):
        for h, (rows, via) in (
            await _buildings_of_houses(client, sorted(set(houses_)), COMPACT_BUILDING_FIELDS)
        ).items():
            out["husnummer_id", h] = (rows, via, [], {})
    if ids := by_kind.get("building_id"):
        by_id = await _by_ids(client, set(ids), COMPACT_BUILDING_FIELDS)
        for b, row in by_id.items():
            out["building_id", b] = ([row], "building_id", [], {})
    if bfes := by_kind.get("bfe_nummer"):
        props = await _properties(client, sorted(set(bfes)), COMPACT_BUILDING_FIELDS, COMPACT_UNIT_FIELDS)
        for bfe, p in props.items():
            if p["found"]:
                out["bfe_nummer", bfe] = (p["buildings"], "bfe", p["units"], {"property_kind": p["kind"]})

    if include_units:
        # Units in each building, except where the entry already has its own (an address's, an owner flat's).
        need = {k: v for k, v in out.items() if not v[2] and k[0] != "adresse_id"}
        ids_ = sorted({r["id_lokalId"] for rows, *_ in need.values() for r in rows})
        units = await _bbr_in(client, "BBR_Enhed", "bygning", ids_, COMPACT_UNIT_FIELDS) if ids_ else []
        in_building: dict[str, list[dict[str, Any]]] = {}
        for u in units:
            in_building.setdefault(u.get("bygning"), []).append(u)
        for k, (rows, via, _, extra) in need.items():
            out[k] = (rows, via, [u for r in rows for u in in_building.get(r["id_lokalId"], [])], extra)
    return out


async def _self_test() -> None:
    async with Client(mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    expected = {
        "address_search",
        "address_get",
        "buildings",
        "units",
        "buildings_in_area",
        "buildings_for_addresses",
        "technical_installations",
    }
    if names != expected:
        sys.exit(f"self-test failed: tools {sorted(names)}, expected {sorted(expected)}")
    solar = translate("tek020Klassifikation", "1230")
    if solar != {"code": "1230", "text": "Solcelleanlæg"}:
        sys.exit(f"self-test failed: tek020 code 1230 translated to {solar}")
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
