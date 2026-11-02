import httpx2
import pytest

from glpi_mcp import retry
from glpi_mcp.retry import RetryTransport
from glpi_mcp.retry import parse_retry_after
from glpi_mcp.retry import shared_transport


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("7", 7.0),
        (" 0.5 ", 0.5),
        ("0", 0.0),
        (None, None),
        ("", None),
        # The HTTP-date form is not handled.
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
        ("-1", None),
        ("inf", None),
        ("nan", None),
    ],
)
def test_parse_retry_after(raw, expected):
    assert parse_retry_after(raw) == expected


async def test_unparsable_retry_after_falls_back_to_backoff():
    statuses = [503, 200]
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.method)
        return httpx2.Response(
            statuses.pop(0), headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
        )

    transport = RetryTransport(httpx2.MockTransport(handler), owns_wrapped=False)
    async with httpx2.AsyncClient(transport=transport) as client:
        response = await client.get("https://glpi.test/api.php/")
    assert response.status_code == 200
    assert seen == ["GET", "GET"]


class Tracking(httpx2.MockTransport):
    def __init__(self):
        super().__init__(lambda _request: httpx2.Response(200))
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("owns", [True, False])
async def test_closing_closes_the_wrapped_transport_only_if_owned(owns):
    inner = Tracking()
    await RetryTransport(inner, owns_wrapped=owns).aclose()
    assert inner.closed is owns


def test_shared_transport_is_one_per_tls_setting(monkeypatch):
    monkeypatch.setattr(retry, "_shared_transports", {})
    verified = shared_transport(True)
    unverified = shared_transport(False)
    assert shared_transport(True) is verified
    assert shared_transport(False) is unverified
    assert verified is not unverified
    assert isinstance(verified, httpx2.AsyncHTTPTransport)
