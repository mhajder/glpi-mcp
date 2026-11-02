import asyncio
from typing import Any

import httpx2
import pytest

from glpi_mcp.glpi_client import AmbiguousWriteError
from glpi_mcp.glpi_client import GlpiAPIError
from glpi_mcp.glpi_client import GlpiClient
from glpi_mcp.glpi_client import GlpiVersionError
from glpi_mcp.glpi_client import MissingTokenError
from glpi_mcp.glpi_client import ResponseTooLargeError
from glpi_mcp.glpi_client import bounded_put
from glpi_mcp.glpi_client import parse_content_range
from glpi_mcp.glpi_client import parse_filename
from glpi_mcp.glpi_client import pick_api_version
from tests.conftest import ACCESS_TOKEN
from tests.conftest import API_VERSIONS_11
from tests.conftest import API_VERSIONS_12
from tests.conftest import FakeGlpi


@pytest.mark.parametrize(
    ("versions", "requested", "expected"),
    [
        (API_VERSIONS_11, "auto", "2.3"),
        (API_VERSIONS_12, "auto", "3.0"),
        (API_VERSIONS_12, "2.3", "2.3"),
        (API_VERSIONS_12, "3.0", "3.0"),
    ],
)
def test_pick_api_version(versions, requested, expected):
    assert pick_api_version(versions, requested) == expected


def test_pick_api_version_needs_glpi_11_0_6():
    """GLPI 11.0.0-11.0.5 offer only 2.0-2.2, which this server cannot use."""
    old = [{"version": "2.0.0"}, {"version": "2.1.0"}, {"version": "2.2.0"}]
    with pytest.raises(GlpiVersionError, match=r"2\.0, 2\.1, 2\.2.*11\.0\.6"):
        pick_api_version(old, "auto")


def test_pick_api_version_refuses_an_unoffered_pin():
    with pytest.raises(GlpiVersionError, match=r"GLPI_API_VERSION=3\.0 is not offered"):
        pick_api_version(API_VERSIONS_11, "3.0")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0-19/250", 250),
        ("items 0-0/1", 1),
        ("*/0", 0),
        ("0--1/0", 0),
        (None, None),
        ("", None),
    ],
)
def test_parse_content_range(raw, expected):
    assert parse_content_range(raw) == expected


async def test_requests_are_pinned_to_the_version(config):
    glpi = FakeGlpi(API_VERSIONS_12, version="3.0")
    glpi.get("Assistance/Ticket/1", payload={"id": 1})
    async with GlpiClient(config, transport=glpi.transport) as client:
        assert await client.get("Assistance/Ticket/1") == {"id": 1}
    [request] = glpi.api_requests()
    assert request.path == "/api.php/v3.0/Assistance/Ticket/1"
    assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"


async def test_password_grant_sends_the_credentials(config, glpi):
    glpi.get("session", payload={"user_id": 2})
    async with GlpiClient(config, transport=glpi.transport) as client:
        await client.get("session")
    [grant] = glpi.sent("POST", "/api.php/token")
    assert grant.body == {
        "grant_type": "password",
        "client_id": "client",
        "client_secret": "secret",
        "username": "glpi",
        "password": "glpi",
        "scope": "api user",
    }


async def test_token_is_cached_across_clients(config, glpi):
    glpi.get("session", payload={})
    for _ in range(3):
        async with GlpiClient(config, transport=glpi.transport) as client:
            await client.get("session")
    assert len(glpi.sent("POST", "/api.php/token")) == 1
    assert len(glpi.sent("GET", "/api.php/")) == 1


async def test_concurrent_calls_share_one_token_request(config, glpi):
    glpi.get("session", payload={})

    async def call():
        async with GlpiClient(config, transport=glpi.transport) as client:
            await client.get("session")

    await asyncio.gather(*(call() for _ in range(5)))
    assert len(glpi.sent("POST", "/api.php/token")) == 1


async def test_expired_token_is_renewed(config, glpi, monkeypatch):
    glpi.get("session", payload={})
    clock = [1000.0]
    monkeypatch.setattr("glpi_mcp.glpi_client.time.monotonic", lambda: clock[0])
    async with GlpiClient(config, transport=glpi.transport) as client:
        await client.get("session")
        clock[0] += 3600
        await client.get("session")
    assert len(glpi.sent("POST", "/api.php/token")) == 2


async def test_refused_token_is_renewed_once(config, glpi):
    glpi.get("session", status=401, payload={"status": "ERROR"})
    glpi.get("session", payload={"user_id": 2})
    async with GlpiClient(config, transport=glpi.transport) as client:
        assert await client.get("session") == {"user_id": 2}
    assert len(glpi.sent("POST", "/api.php/token")) == 2


async def test_token_glpi_calls_invalid_is_renewed(config, glpi):
    """GLPI answers 400 'Invalid OAuth token' for an expired or revoked token."""
    glpi.get(
        "session",
        status=400,
        payload={"status": "ERROR_INVALID_PARAMETER", "title": "Invalid OAuth token"},
    )
    glpi.get("session", payload={"user_id": 2})
    async with GlpiClient(config, transport=glpi.transport) as client:
        assert await client.get("session") == {"user_id": 2}
    assert len(glpi.sent("POST", "/api.php/token")) == 2


async def test_other_400_is_not_a_token_problem(config, glpi):
    glpi.get("session", status=400, payload={"status": "ERROR", "title": "Bad filter"})
    async with GlpiClient(config, transport=glpi.transport) as client:
        with pytest.raises(GlpiAPIError):
            await client.get("session")
    assert len(glpi.sent("POST", "/api.php/token")) == 1


async def test_second_refusal_is_reported(config, glpi):
    glpi.get("session", status=401, payload={"status": "ERROR", "title": "nope"})
    async with GlpiClient(config, transport=glpi.transport) as client:
        with pytest.raises(GlpiAPIError) as info:
            await client.get("session")
    assert info.value.status == 401
    assert len(glpi.sent("POST", "/api.php/token")) == 2


async def test_refused_password_grant_keeps_the_oauth_error(config, glpi):
    glpi.routes[("POST", "/api.php/token")].clear()
    glpi.post(
        "/api.php/token",
        status=400,
        payload={"error": "invalid_grant", "error_description": "bad password"},
    )
    async with GlpiClient(config, transport=glpi.transport) as client:
        with pytest.raises(GlpiAPIError) as info:
            await client.get("session")
    assert info.value.error_code == "invalid_grant"
    assert info.value.path == "token"


async def test_caller_token_is_used_as_is(config, glpi):
    glpi.get("session", payload={})
    async with GlpiClient(config, token="user-token", transport=glpi.transport) as c:
        await c.get("session")
    assert glpi.sent("POST", "/api.php/token") == []
    assert (
        glpi.sent("GET", "session")[0].headers["Authorization"] == "Bearer user-token"
    )


async def test_caller_token_is_not_renewed_on_401(config, glpi):
    glpi.get("session", status=401, payload={"status": "ERROR"})
    async with GlpiClient(config, token="user-token", transport=glpi.transport) as c:
        with pytest.raises(GlpiAPIError):
            await c.get("session")
    assert glpi.sent("POST", "/api.php/token") == []
    assert len(glpi.sent("GET", "session")) == 1


async def test_passthrough_without_a_token_never_uses_the_password_grant(config, glpi):
    passthrough = config.model_copy(update={"token_passthrough": True})
    with pytest.raises(MissingTokenError):
        async with GlpiClient(passthrough, transport=glpi.transport):
            pass
    assert glpi.requests == []


async def test_scope_headers(config, glpi):
    glpi.get("session", payload={})
    scoped = config.model_copy(
        update={"entity_id": 3, "entity_recursive": True, "profile_id": 4}
    )
    async with GlpiClient(scoped, transport=glpi.transport) as client:
        await client.get("session")
    headers = glpi.sent("GET", "session")[0].headers
    assert headers["GLPI-Entity"] == "3"
    assert headers["GLPI-Entity-Recursive"] == "true"
    assert headers["GLPI-Profile"] == "4"


async def test_no_scope_headers_by_default(config, glpi):
    glpi.get("session", payload={})
    async with GlpiClient(config, transport=glpi.transport) as client:
        await client.get("session")
    headers = glpi.sent("GET", "session")[0].headers
    assert "GLPI-Entity" not in headers
    assert "GLPI-Profile" not in headers


async def test_glpi_10_is_refused(config):
    glpi = FakeGlpi()
    glpi.routes[("GET", "/api.php/")] = [(404, None, {})]
    async with GlpiClient(config, transport=glpi.transport) as client:
        with pytest.raises(GlpiVersionError, match=r"11\.0\.6"):
            await client.get("session")


async def test_collection_reads_total_from_content_range(config, glpi):
    glpi.get(
        "Assets/Computer",
        status=206,
        payload=[{"id": 1}, {"id": 2}],
        headers={"Content-Range": "0-1/7"},
    )
    async with GlpiClient(config, transport=glpi.transport) as client:
        result = await client.collection(
            "Assets/Computer", filter="name==a", sort="id:asc", start=0, limit=2
        )
    assert result.rows == [{"id": 1}, {"id": 2}]
    assert result.total == 7
    assert result.has_more
    [request] = glpi.sent("GET", "Assets/Computer")
    assert dict(request.query) == {
        "filter": "name==a",
        "sort": "id:asc",
        "start": "0",
        "limit": "2",
    }


async def test_collection_without_content_range(config, glpi):
    glpi.get("Assets/Computer", payload=[{"id": 1}])
    async with GlpiClient(config, transport=glpi.transport) as client:
        result = await client.collection("Assets/Computer", limit=5)
    assert result.total is None
    assert not result.has_more
    assert "filter" not in dict(glpi.sent("GET", "Assets/Computer")[0].query)


async def test_error_body_is_kept(config, glpi):
    glpi.get(
        "Assistance/Ticket/9",
        status=404,
        payload={"status": "ERROR_ITEM_NOT_FOUND", "title": "Not found"},
    )
    async with GlpiClient(config, transport=glpi.transport) as client:
        with pytest.raises(GlpiAPIError) as info:
            await client.get("Assistance/Ticket/9")
    assert info.value.status == 404
    assert info.value.error_code == "ERROR_ITEM_NOT_FOUND"


async def test_read_is_retried_on_503(config, glpi):
    glpi.get("session", status=503, payload={"status": "ERROR"})
    glpi.get("session", payload={"user_id": 2})
    async with GlpiClient(config, transport=glpi.transport) as client:
        assert await client.get("session") == {"user_id": 2}


async def test_create_is_not_retried_on_502(config, glpi):
    """GLPI may have created the item before the proxy in front of it failed."""
    glpi.post("Assistance/Ticket", status=502, payload={"status": "ERROR"})
    async with GlpiClient(config, transport=glpi.transport) as client:
        with pytest.raises(GlpiAPIError):
            await client.post("Assistance/Ticket", json={"name": "x"})
    assert len(glpi.sent("POST", "Assistance/Ticket")) == 1


async def test_write_timeout_is_ambiguous(config):
    glpi = FakeGlpi()

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/Assistance/Ticket"):
            raise httpx2.ReadTimeout("timed out", request=request)
        return glpi.handle(request)

    transport = httpx2.MockTransport(handler)
    async with GlpiClient(config, transport=transport) as client:
        with pytest.raises(AmbiguousWriteError, match="MAY have been applied"):
            await client.post("Assistance/Ticket", json={"name": "x"})


async def test_download_returns_bytes_and_filename(config, glpi):
    glpi.get(
        "Management/Document/5/Download",
        content=b"hello",
        headers={
            "Content-Type": "text/plain",
            "Content-Disposition": 'attachment; filename="notes.txt"',
        },
    )
    async with GlpiClient(config, transport=glpi.transport) as client:
        download = await client.download("Management/Document/5/Download")
    assert download.content == b"hello"
    assert download.filename == "notes.txt"
    assert download.content_type == "text/plain"


async def test_openapi_is_cached(config, glpi):
    glpi.get("doc.json", payload={"paths": {}})
    for _ in range(2):
        async with GlpiClient(config, transport=glpi.transport) as client:
            await client.openapi()
    assert len(glpi.sent("GET", "doc.json")) == 1


# ------------------------------------------------- code-review regressions


async def test_client_without_retry_does_not_close_the_shared_transport(config, glpi):
    """A token check (retry=False) must not tear down the shared pool."""
    closed = []

    class Tracking(httpx2.MockTransport):
        async def aclose(self):
            closed.append(True)

    tracked = Tracking(glpi.handle)
    glpi.get("session", payload={})
    async with GlpiClient(config, token="t", transport=tracked, retry=False) as client:
        await client.get("session")
    assert closed == []


async def test_long_retry_after_is_returned_not_retried(config, glpi):
    glpi.get("session", status=429, payload=None, headers={"Retry-After": "60"})
    async with GlpiClient(config, transport=glpi.transport) as client:
        with pytest.raises(GlpiAPIError) as info:
            await client.get("session")
    assert info.value.retry_after == 60
    assert len(glpi.sent("GET", "session")) == 1


async def test_download_refuses_files_over_max_bytes(config, glpi):
    glpi.get("Management/Document/5/Download", content=b"x" * 100)
    async with GlpiClient(config, transport=glpi.transport) as client:
        with pytest.raises(ResponseTooLargeError):
            await client.download("Management/Document/5/Download", max_bytes=10)


@pytest.mark.parametrize(
    ("disposition", "expected"),
    [
        ('attachment; filename="notes.txt"', "notes.txt"),
        ("attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf", "résumé.pdf"),
        ("attachment; filename*=utf-8''a.pdf", "a.pdf"),
        ("attachment; filename=\"x.pdf\"; filename*=UTF-8''y.pdf", "y.pdf"),
        ("inline", None),
    ],
)
def test_parse_filename(disposition, expected):
    assert parse_filename(disposition) == expected


async def test_retry_wait_past_the_deadline_returns_the_answer(config, glpi):
    """A refused write must not surface as an ambiguous one by timing out mid-wait."""
    glpi.post(
        "Assistance/Ticket", status=429, payload=None, headers={"Retry-After": "5"}
    )
    short = config.model_copy(update={"timeout": 2})
    async with GlpiClient(short, transport=glpi.transport) as client:
        with pytest.raises(GlpiAPIError) as info:
            await client.post("Assistance/Ticket", json={"name": "x"})
    assert info.value.status == 429
    assert len(glpi.sent("POST", "Assistance/Ticket")) == 1


async def test_read_only_post_timeout_is_not_ambiguous(config):
    glpi = FakeGlpi()

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/GraphQL"):
            raise httpx2.ReadTimeout("timed out", request=request)
        return glpi.handle(request)

    transport = httpx2.MockTransport(handler)
    async with GlpiClient(config, transport=transport) as client:
        with pytest.raises(httpx2.ReadTimeout):
            await client.post("GraphQL", json={"query": "{}"}, read_only=True)


async def test_retry_leaves_time_for_the_retried_request(config):
    """A retry that fits the wait but not the request itself is not sent."""
    calls = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        if request.url.path.endswith("/Assistance/Ticket"):
            calls += 1
            await asyncio.sleep(0.6)
            return httpx2.Response(429, headers={"Retry-After": "0.5"})
        return FakeGlpi().handle(request)

    short = config.model_copy(update={"timeout": 1.5})
    transport = httpx2.MockTransport(handler)
    async with GlpiClient(short, transport=transport) as client:
        with pytest.raises(GlpiAPIError) as info:
            await client.post("Assistance/Ticket", json={"name": "x"})
    assert info.value.status == 429
    assert calls == 1


async def test_read_only_post_is_retried_on_5xx(config, glpi):
    glpi.post("GraphQL", status=503, payload=None)
    glpi.post("GraphQL", payload={"data": {}})
    async with GlpiClient(config, transport=glpi.transport) as client:
        assert await client.post("GraphQL", json={"query": "{}"}, read_only=True) == {
            "data": {}
        }
    assert len(glpi.sent("POST", "GraphQL")) == 2


# ------------------------------------------------- one deadline per request


def slow_glpi(glpi: FakeGlpi, delays: dict[str, float]) -> httpx2.MockTransport:
    """Wrap FakeGlpi so requests whose path ends with a key wait that long."""

    async def handler(request: httpx2.Request) -> httpx2.Response:
        for suffix, delay in delays.items():
            if request.url.path.endswith(suffix):
                await asyncio.sleep(delay)
        return glpi.handle(request)

    return httpx2.MockTransport(handler)


async def test_timeout_covers_version_lookup_token_and_request(config, glpi):
    """GLPI_TIMEOUT is the total for one request, not a budget per step."""
    glpi.get("session", payload={})
    transport = slow_glpi(glpi, {"/api.php/": 0.3, "/token": 0.3, "/session": 0.3})
    quick = config.model_copy(update={"timeout": 0.5})
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with GlpiClient(quick, transport=transport) as client:
        with pytest.raises(TimeoutError, match=r"within 0\.5s"):
            await client.get("session")
    assert loop.time() - started < 0.8


async def test_write_timeout_before_sending_is_not_ambiguous(config, glpi):
    """A create that timed out while getting a token was never sent."""
    transport = slow_glpi(glpi, {"/token": 1})
    quick = config.model_copy(update={"timeout": 0.2})
    async with GlpiClient(quick, transport=transport) as client:
        with pytest.raises(TimeoutError):
            await client.post("Assistance/Ticket", json={"name": "x"})
    assert glpi.sent("POST", "Assistance/Ticket") == []


async def test_write_timeout_after_sending_is_ambiguous(config, glpi):
    glpi.post("Assistance/Ticket", status=201, payload={"id": 1})
    transport = slow_glpi(glpi, {"/Assistance/Ticket": 1})
    quick = config.model_copy(update={"timeout": 0.3})
    async with GlpiClient(quick, transport=transport) as client:
        with pytest.raises(AmbiguousWriteError, match="MAY have been applied"):
            await client.post("Assistance/Ticket", json={"name": "x"})


# ------------------------------------------------- replayed DELETE


async def test_delete_replayed_after_5xx_and_gone_is_success(config, glpi):
    """The first DELETE went through but the proxy lost GLPI's answer."""
    glpi.delete("Assets/Computer/3", status=504, payload=None)
    glpi.delete("Assets/Computer/3", status=404, payload={"status": "ERROR"})
    async with GlpiClient(config, transport=glpi.transport) as client:
        await client.delete("Assets/Computer/3")
    assert len(glpi.sent("DELETE", "Assets/Computer/3")) == 2


async def test_delete_of_a_missing_item_is_still_404(config, glpi):
    glpi.delete("Assets/Computer/3", status=404, payload={"status": "ERROR"})
    async with GlpiClient(config, transport=glpi.transport) as client:
        with pytest.raises(GlpiAPIError) as info:
            await client.delete("Assets/Computer/3")
    assert info.value.status == 404


async def test_delete_404_after_429_is_still_404(config, glpi):
    """A 429 means GLPI never handled the first DELETE."""
    glpi.delete("Assets/Computer/3", status=429, payload=None)
    glpi.delete("Assets/Computer/3", status=404, payload={"status": "ERROR"})
    async with GlpiClient(config, transport=glpi.transport) as client:
        with pytest.raises(GlpiAPIError):
            await client.delete("Assets/Computer/3")


async def test_timeout_renewing_a_refused_token_is_not_ambiguous(config, glpi):
    """GLPI refused the write with 401; nothing was applied before the timeout."""
    glpi.post("Assistance/Ticket", status=401, payload={"status": "ERROR"})
    grants = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/token"):
            grants.append(True)
            if len(grants) > 1:
                await asyncio.sleep(1)
        return glpi.handle(request)

    quick = config.model_copy(update={"timeout": 0.3})
    async with GlpiClient(quick, transport=httpx2.MockTransport(handler)) as client:
        with pytest.raises(TimeoutError):
            await client.post("Assistance/Ticket", json={"name": "x"})
    assert len(grants) == 2


# ------------------------------------------------- helpers and edge answers


def test_bounded_put_drops_the_oldest_entries():
    cache = {"a": 1, "b": 2}
    bounded_put(cache, "a", 3, limit=2)
    assert list(cache.items()) == [("b", 2), ("a", 3)]
    bounded_put(cache, "c", 4, limit=2)
    assert list(cache.items()) == [("a", 3), ("c", 4)]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"status": "ERROR_ITEM_NOT_FOUND"}, "ERROR_ITEM_NOT_FOUND"),
        ({"error": "invalid_grant"}, "invalid_grant"),
        ({"title": "no code"}, None),
        ("<html>Bad Gateway</html>", None),
        (None, None),
    ],
)
def test_error_code(body, expected):
    assert GlpiAPIError(502, "GET", "session", body).error_code == expected


def test_parse_filename_with_an_unknown_charset_still_decodes():
    assert parse_filename("attachment; filename*=x-nope''a%20b.pdf") == "a b.pdf"


def test_pick_api_version_skips_malformed_entries():
    # GLPI answers with JSON, so the list may hold anything.
    offered: list[Any] = ["2.3.0", None, {"version": "garbage"}, {"version": "2.3.1"}]
    assert pick_api_version(offered, "auto") == "2.3"


async def test_empty_explicit_token_is_refused(config, glpi):
    with pytest.raises(MissingTokenError, match="empty GLPI token"):
        async with GlpiClient(config, token="", transport=glpi.transport):
            pass
    assert glpi.requests == []


async def test_password_grant_needs_the_credentials(config, glpi):
    incomplete = config.model_copy(update={"password": None})
    async with GlpiClient(incomplete, transport=glpi.transport) as client:
        with pytest.raises(MissingTokenError, match="not configured: GLPI_PASSWORD"):
            await client.get("session")
    assert glpi.sent("POST", "/api.php/token") == []
    assert glpi.sent("GET", "session") == []


async def test_password_grant_without_access_token_is_an_error(config, glpi):
    glpi.routes[("POST", "/api.php/token")].clear()
    glpi.post("/api.php/token", payload={"token_type": "Bearer"})
    async with GlpiClient(config, transport=glpi.transport) as client:
        with pytest.raises(GlpiAPIError, match="No access_token in response"):
            await client.get("session")
    assert glpi.sent("GET", "session") == []


async def test_concurrent_calls_wait_for_the_token_being_fetched(config, glpi):
    """Calls queued behind a slow grant reuse its token instead of asking again."""
    glpi.get("session", payload={})
    transport = slow_glpi(glpi, {"/token": 0.05})

    async def call():
        async with GlpiClient(config, transport=transport) as client:
            await client.get("session")

    await asyncio.gather(*(call() for _ in range(3)))
    assert len(glpi.sent("POST", "/api.php/token")) == 1
    assert len(glpi.sent("GET", "session")) == 3


async def test_non_json_body_is_returned_as_truncated_text(config, glpi):
    glpi.get("session", content=b"x" * 3000, headers={"Content-Type": "text/html"})
    async with GlpiClient(config, transport=glpi.transport) as client:
        assert await client.get("session") == "x" * 2000


async def test_patch_sends_the_json_body(config, glpi):
    glpi.patch("Assets/Computer/3", payload={"id": 3})
    async with GlpiClient(config, transport=glpi.transport) as client:
        assert await client.patch("Assets/Computer/3", json={"name": "pc"}) == {"id": 3}
    [request] = glpi.sent("PATCH", "Assets/Computer/3")
    assert request.body == {"name": "pc"}


async def test_client_outside_its_context_is_refused(config, glpi):
    client = GlpiClient(config, transport=glpi.transport)
    with pytest.raises(RuntimeError, match="async context manager"):
        await client.get("session")
    with pytest.raises(RuntimeError, match="async context manager"):
        await client.api_versions()
    assert glpi.requests == []


# ------------------------------------------------- transport failures


async def test_write_that_never_connected_is_not_ambiguous(config):
    """A connect error means nothing reached GLPI, so a retry is safe."""
    glpi = FakeGlpi()

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/Assistance/Ticket"):
            raise httpx2.ConnectError("connection refused", request=request)
        return glpi.handle(request)

    async with GlpiClient(config, transport=httpx2.MockTransport(handler)) as client:
        with pytest.raises(httpx2.ConnectError):
            await client.post("Assistance/Ticket", json={"name": "x"})


async def test_version_lookup_timeout_names_the_step(config, glpi):
    transport = slow_glpi(glpi, {"/api.php/": 1})
    quick = config.model_copy(update={"timeout": 0.1})
    async with GlpiClient(quick, transport=transport) as client:
        with pytest.raises(TimeoutError, match=r"list its API versions within 0\.1s"):
            await client.api_version()


async def test_version_lookup_error_is_reported(config):
    glpi = FakeGlpi()
    glpi.routes[("GET", "/api.php/")] = [(403, {"status": "ERROR_FORBIDDEN"}, {})]
    async with GlpiClient(config, transport=glpi.transport) as client:
        with pytest.raises(GlpiAPIError) as info:
            await client.get("session")
    assert info.value.status == 403
    assert info.value.path == "/"
    assert info.value.error_code == "ERROR_FORBIDDEN"


async def test_openapi_must_be_an_object(config, glpi):
    glpi.get("doc.json", payload=["not", "a", "document"])
    async with GlpiClient(config, transport=glpi.transport) as client:
        with pytest.raises(GlpiAPIError, match="Not an OpenAPI document"):
            await client.openapi()


# ------------------------------------------------- bounded downloads


async def test_download_error_keeps_the_body(config, glpi):
    glpi.get(
        "Management/Document/5/Download",
        status=404,
        payload={"status": "ERROR_ITEM_NOT_FOUND"},
    )
    async with GlpiClient(config, transport=glpi.transport) as client:
        with pytest.raises(GlpiAPIError) as info:
            await client.download("Management/Document/5/Download", max_bytes=10)
    assert info.value.status == 404
    assert info.value.error_code == "ERROR_ITEM_NOT_FOUND"


async def test_download_without_length_is_cut_off_at_max_bytes(config):
    """A chunked body has no Content-Length, so the size is counted as it streams."""
    glpi = FakeGlpi()
    chunks_sent = []

    async def body():
        for _ in range(100):
            chunks_sent.append(True)
            yield b"x" * 4

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/Download"):
            return httpx2.Response(200, content=body())
        return glpi.handle(request)

    async with GlpiClient(config, transport=httpx2.MockTransport(handler)) as client:
        with pytest.raises(ResponseTooLargeError, match="more bytes than that"):
            await client.download("Management/Document/5/Download", max_bytes=10)
    assert len(chunks_sent) < 100


async def test_download_within_max_bytes_is_returned(config):
    glpi = FakeGlpi()

    async def body():
        yield b"he"
        yield b"llo"

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/Download"):
            return httpx2.Response(
                200, content=body(), headers={"Content-Type": "text/plain"}
            )
        return glpi.handle(request)

    async with GlpiClient(config, transport=httpx2.MockTransport(handler)) as client:
        download = await client.download("Management/Document/5/Download", max_bytes=10)
    assert download.content == b"hello"
    assert download.content_type == "text/plain"


# ------------------------------------------------- redirects


def redirecting_glpi(glpi: FakeGlpi, statuses: dict[str, int]) -> httpx2.MockTransport:
    """Send http:// requests to https://, with the given status per path suffix.

    Paths without a listed suffix get a 308, which keeps the method.
    """

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.scheme == "http":
            status = next(
                (
                    s
                    for suffix, s in statuses.items()
                    if request.url.path.endswith(suffix)
                ),
                308,
            )
            target = request.url.copy_with(scheme="https")
            return httpx2.Response(status, headers={"Location": str(target)})
        return glpi.handle(request)

    return httpx2.MockTransport(handler)


async def test_redirect_that_keeps_the_method_works_and_warns_once(
    config, glpi, caplog
):
    glpi.get("session", payload={"user_id": 2})
    plain = config.model_copy(update={"glpi_url": "http://glpi.test"})
    transport = redirecting_glpi(glpi, {})
    async with GlpiClient(plain, transport=transport) as client:
        assert await client.get("session") == {"user_id": 2}
        assert await client.get("session") == {"user_id": 2}
    # The token grant went through the redirect as a POST.
    [grant] = glpi.sent("POST", "/api.php/token")
    assert grant.body["grant_type"] == "password"
    warnings = [r for r in caplog.records if "GLPI redirects" in r.getMessage()]
    assert len(warnings) == 1
    assert "http://glpi.test/api.php/token to https://glpi.test/api.php/token" in (
        warnings[0].getMessage()
    )


async def test_write_turned_into_a_get_by_a_redirect_is_refused(config, glpi):
    plain = config.model_copy(update={"glpi_url": "http://glpi.test"})
    transport = redirecting_glpi(glpi, {"/Assistance/Ticket": 301})
    async with GlpiClient(plain, transport=transport) as client:
        with pytest.raises(ValueError, match=r"turned into a GET.*NOT applied"):
            await client.post("Assistance/Ticket", json={"name": "x"})
    assert glpi.sent("POST", "Assistance/Ticket") == []
