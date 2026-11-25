"""A fake BBR GraphQL service for the tests: rows per entity, filtered by the query's eq/in conditions."""

import json
import re

import httpx2


def page(entity, nodes):
    return httpx2.Response(200, json={"data": {entity: {"nodes": nodes, "pageInfo": {"hasNextPage": False}}}})


def matches(row, where):
    for field, cond in where.items():
        if field == "and":
            if not all(matches(row, c) for c in cond):
                return False
            continue
        value = str(row.get(field))
        if "eq" in cond and value != str(cond["eq"]):
            return False
        if "in" in cond and value not in {str(x) for x in cond["in"]}:
            return False
    return True


class FakeBBR:
    def __init__(self, **tables):
        self.tables = tables
        self.calls = []

    def __call__(self, request):
        body = json.loads(request.content)
        entity = re.search(r"(BBR_\w+)\(", body["query"]).group(1)
        where = body["variables"]["where"]
        self.calls.append((entity, where))
        return page(entity, [r for r in self.tables.get(entity, []) if matches(r, where)])

    def asked(self, entity):
        return [where for e, where in self.calls if e == entity]
