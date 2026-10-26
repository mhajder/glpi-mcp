import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest
from fastmcp import Client
from fastmcp import FastMCP

from glpi_mcp import retry
from glpi_mcp.glpi_client import GlpiClient
from glpi_mcp.models import GlpiConfig
from glpi_mcp.tools import register_tools

# Returned for any request no test registered; not 404 or 401, so it never
# triggers the client's version or token handling by accident.
UNMOCKED_STATUS = 418

API_VERSIONS_11 = [
    {
        "api_version": "1",
        "version": "1.0.0",
        "endpoint": "https://glpi.test/api.php/v1",
    },
    {
        "api_version": "2",
        "version": "2.2.0",
        "endpoint": "https://glpi.test/api.php/v2.2",
        "deprecated": True,
    },
    {
        "api_version": "2",
        "version": "2.3.0",
        "endpoint": "https://glpi.test/api.php/v2.3",
    },
]
API_VERSIONS_12 = [
    {
        "api_version": "1",
        "version": "1.0.0",
        "endpoint": "https://glpi.test/api.php/v1",
    },
    {
        "api_version": "2",
        "version": "2.3.0",
        "endpoint": "https://glpi.test/api.php/v2.3",
    },
    {
        "api_version": "3",
        "version": "3.0.0",
        "endpoint": "https://glpi.test/api.php/v3.0",
    },
]
ACCESS_TOKEN = "glpi-access-token"  # noqa: S105


@dataclass
class Recorded:
    method: str
    path: str
    query: list[tuple[str, str]]
    headers: httpx2.Headers
    body: Any

    def param(self, name: str) -> str | None:
        return dict(self.query).get(name)


class FakeGlpi:
    """Answers GLPI High-Level API calls with canned responses.

    Serves ``GET /api.php/`` (the version list) and ``POST /api.php/token`` by
    default, so tests only register the endpoints they exercise. Paths given
    to ``add`` are relative to the version root (``Assistance/Ticket``) unless
    they start with ``/``.
    """

    def __init__(self, versions: list[dict] | None = None, version: str = "2.3"):
        self.version = version
        self.routes: dict[tuple[str, str], list[tuple[int, Any, dict]]] = defaultdict(
            list
        )
        self.requests: list[Recorded] = []
        self.transport = httpx2.MockTransport(self.handle)
        self.add(
            "GET",
            "/api.php/",
            payload={
                "message": "Welcome to GLPI API",
                "api_versions": versions or API_VERSIONS_11,
            },
        )
        self.add(
            "POST",
            "/api.php/token",
            payload={
                "token_type": "Bearer",
                "expires_in": 3600,
                "access_token": ACCESS_TOKEN,
            },
        )

    def path(self, path: str) -> str:
        if path.startswith("/"):
            return path
        return f"/api.php/v{self.version}/{path.strip('/')}"

    def add(
        self,
        method: str,
        path: str,
        *,
        status: int = 200,
        payload: Any = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ):
        """Queue a response. The last response for a route repeats forever."""
        extra = dict(headers or {})
        if content is not None:
            extra["__content__"] = content  # type: ignore[assignment]
        self.routes[(method, self.path(path))].append((status, payload, extra))

    def get(self, path: str, **kwargs):
        self.add("GET", path, **kwargs)

    def post(self, path: str, **kwargs):
        self.add("POST", path, **kwargs)

    def patch(self, path: str, **kwargs):
        self.add("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs):
        self.add("DELETE", path, **kwargs)

    def sent(self, method: str, path: str | None = None) -> list[Recorded]:
        wanted = self.path(path) if path else None
        return [
            r
            for r in self.requests
            if r.method == method and (wanted is None or r.path == wanted)
        ]

    def api_requests(self) -> list[Recorded]:
        """Requests other than the version list and token grant."""
        return [
            r for r in self.requests if r.path not in ("/api.php/", "/api.php/token")
        ]

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        content_type = request.headers.get("Content-Type", "")
        body: Any = None
        if request.content:
            if content_type.startswith("application/json"):
                body = json.loads(request.content)
            else:
                body = dict(httpx2.QueryParams(request.content.decode()).multi_items())
        self.requests.append(
            Recorded(
                request.method,
                path,
                list(request.url.params.multi_items()),
                request.headers,
                body,
            )
        )
        queue = self.routes.get((request.method, path))
        if not queue:
            return httpx2.Response(UNMOCKED_STATUS, json={"detail": "not mocked"})
        status, payload, headers = queue.pop(0) if len(queue) > 1 else queue[0]
        headers = dict(headers)
        content = headers.pop("__content__", None)
        if content is not None:
            return httpx2.Response(status, content=content, headers=headers)
        if status == 204 or payload is None:
            return httpx2.Response(status, headers=headers)
        return httpx2.Response(status, json=payload, headers=headers)


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    GlpiClient.reset_cache()
    monkeypatch.setattr(retry, "BASE_BACKOFF", 0)
    yield
    GlpiClient.reset_cache()


@pytest.fixture
def config() -> GlpiConfig:
    return GlpiConfig(
        glpi_url="https://glpi.test",
        client_id="client",
        client_secret="secret",
        username="glpi",
        password="glpi",
    )


@pytest.fixture
def glpi() -> FakeGlpi:
    return FakeGlpi()


def build_server(
    config: GlpiConfig,
    transport: httpx2.AsyncBaseTransport,
    *,
    read_only: bool = False,
) -> FastMCP:
    mcp = FastMCP("test")
    register_tools(mcp, config, transport)
    if read_only:
        mcp.enable(tags={"read-only"}, only=True)
    return mcp


@pytest.fixture
def client_for(glpi):
    def factory(config: GlpiConfig, **kwargs) -> Client:
        return Client(build_server(config, glpi.transport, **kwargs))

    return factory
