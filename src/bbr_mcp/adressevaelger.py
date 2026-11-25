"""Client for Adressevælger (https://adressevaelger.dk), Klimadatastyrelsen's replacement for DAWA autocomplete."""

import os
from typing import Any

import httpx2

BASE_URL = os.environ.get("ADRESSEVAELGER_URL", "https://adressevaelger.dk")
# Adressevælger has no user accounts yet; Klimadatastyrelsen asks everyone to send this shared token.
DEFAULT_TOKEN = "adressevaelger123"
TIMEOUT = 20.0

LEVELS = {"adresse": "adresser", "husnummer": "husnumre"}


class AdressevaelgerError(Exception):
    pass


def _token() -> str:
    return os.environ.get("ADRESSEVAELGER_TOKEN", "").strip() or DEFAULT_TOKEN


async def _get(client: httpx2.AsyncClient, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    try:
        response = await client.get(f"{BASE_URL}/{path}", params={**(params or {}), "token": _token()}, timeout=TIMEOUT)
    except httpx2.TransportError as e:
        raise AdressevaelgerError(f"Adressevælger unreachable: {e!r}") from None
    if response.status_code == 404:
        return None
    try:
        payload = response.json()
    except ValueError:
        raise AdressevaelgerError(f"Adressevælger answered {response.status_code} with no JSON") from None
    if response.status_code != 200 or payload.get("status") != "ok":
        detail = payload.get("beskrivelse") or payload.get("status") or response.status_code
        raise AdressevaelgerError(f"Adressevælger: {detail}")
    return payload


async def search(client: httpx2.AsyncClient, text: str, level: str, limit: int) -> list[dict[str, Any]]:
    payload = await _get(client, f"{LEVELS[level]}/soeg", {"tekst": text, "maksimum": limit})
    return (payload or {}).get("fund") or []


# Adressevælger's largest page; it has no paging, so a search never returns more.
MAX_HITS = 100


async def streets(client: httpx2.AsyncClient, text: str) -> list[dict[str, Any]]:
    """Roads within a postcode matching `text` (type navngivenvejpostnummer), each with its number of entrances
    (antal_husnumre)."""
    payload = await _get(client, "soeg", {"tekst": text, "maal": "navngivenvejpostnummer"})
    return [h for h in (payload or {}).get("fund") or [] if h.get("type") == "navngivenvejpostnummer"]


async def entrances(client: httpx2.AsyncClient, road: str, postcode: str) -> list[dict[str, Any]]:
    """The entrances (husnumre) of one road in one postcode, by house number: at most MAX_HITS."""
    payload = await _get(client, "husnumre/soeg", {"vejnavn": road, "postnummer": postcode, "maksimum": MAX_HITS})
    return [h for h in (payload or {}).get("fund") or [] if h.get("type") == "husnummer" and h.get("vejnavn") == road]


async def get(client: httpx2.AsyncClient, level: str, id: str) -> dict[str, Any] | None:
    payload = await _get(client, f"{LEVELS[level]}/{id}")
    return None if payload is None else payload.get(level)
