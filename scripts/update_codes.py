"""Regenerate src/bbr_mcp/codes.json from the official BBR code lists at teknik.bbr.dk.

    uv run scripts/update_codes.py

Each list is an HTML page with one <li>CODE - Text</li> per code inside a `kodeliste-list` block. The script fails if
a page has no such block or no codes, so a change on the site shows up here, not as missing texts at run time.
"""

import html
import json
import re
import sys
from datetime import date

import httpx

from bbr_mcp.codes import CODES_FILE, FIELD_LISTS

SOURCE = "https://teknik.bbr.dk/kodelister"
ITEM = re.compile(r"<li[^>]*>(.*?)</li>", re.S)
CODE = re.compile(r"^([0-9A-Za-z]{1,6})\s+-\s+(.+)$", re.S)


def fetch_list(client: httpx.Client, name: str) -> dict[str, str]:
    response = client.get(f"{SOURCE}/0/1/0/{name}")
    response.raise_for_status()
    start = response.text.find("kodeliste-list")
    if start < 0:
        sys.exit(f"{name}: no kodeliste-list block on the page")
    end = response.text.find("</ul>", start)
    codes = {}
    for item in ITEM.findall(response.text[start:end]):
        text = " ".join(html.unescape(re.sub(r"<[^>]+>", "", item)).split())
        match = CODE.match(text)
        if match:
            codes[match.group(1)] = match.group(2)
    if not codes:
        sys.exit(f"{name}: no codes found")
    return codes


def main() -> None:
    lists = {}
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for name in sorted(set(FIELD_LISTS.values())):
            lists[name] = fetch_list(client, name)
            print(f"{name}: {len(lists[name])} codes")
    data = {"_source": SOURCE, "_fetched": date.today().isoformat(), **lists}
    CODES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    print(f"wrote {CODES_FILE}")


if __name__ == "__main__":
    main()
