"""A small stand-in for respx on httpx2 (respx patches only httpx): routes by method and URL, answered through
httpx2.MockTransport, which conftest.py gives every client the server makes.

    import mock_http as respx
    respx.post(URL).mock(return_value=httpx2.Response(200, json=...))   # or side_effect=callable | list | exception
    route.calls[0].request
"""

from dataclasses import dataclass, field
from typing import Any

import httpx2


@dataclass
class Call:
    request: httpx2.Request
    response: httpx2.Response | None = None


@dataclass
class Route:
    method: str
    url: str
    return_value: httpx2.Response | None = None
    side_effect: Any = None
    calls: list[Call] = field(default_factory=list)

    def mock(self, return_value: httpx2.Response | None = None, side_effect: Any = None) -> "Route":
        self.return_value, self.side_effect = return_value, side_effect
        if isinstance(side_effect, (list, tuple)):
            self.side_effect = iter(side_effect)
        return self

    def matches(self, request: httpx2.Request) -> bool:
        # The URL without its query matches, as a respx pattern without params does.
        return request.method == self.method and str(request.url.copy_with(query=None)) == self.url

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        effect = self.side_effect
        if effect is not None and hasattr(effect, "__next__"):
            effect = next(effect)
        if isinstance(effect, BaseException) or (isinstance(effect, type) and issubclass(effect, BaseException)):
            raise effect
        response = effect(request) if callable(effect) else effect if effect is not None else self.return_value
        if response is None:
            response = httpx2.Response(200)
        self.calls.append(Call(request, response))
        return response


class Router:
    def __init__(self) -> None:
        self.routes: list[Route] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        request.read()
        for route in reversed(self.routes):
            if route.matches(request):
                return route.respond(request)
        raise AssertionError(f"no mocked route for {request.method} {request.url.copy_with(query=None)}")


ROUTER = Router()


def get(url: str) -> Route:
    ROUTER.routes.append(route := Route("GET", url))
    return route


def post(url: str) -> Route:
    ROUTER.routes.append(route := Route("POST", url))
    return route


def mock(test):
    """respx.mock's place as a decorator: conftest.py already mocks every non-live test."""
    return test
