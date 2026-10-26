"""
Retrying transport for the GLPI API.

Retries live in the HTTP layer rather than in each tool: it is the one place
every request already passes through, and it keeps ``Retry-After`` next to the
response that carries it.

Only responses that can plausibly succeed on a second attempt are retried - 429
and 5xx. A 5xx on a POST is *not* retried: GLPI may have committed the create
before the proxy in front of it failed, and replaying the request would create a
duplicate. A 429 is always safe to replay, because the request was rejected
before GLPI handled it.

A DELETE is replayed after a 5xx, but GLPI may have deleted the item before the
proxy failed: a 404 to the replay then means the delete went through, and is
reported as success rather than as "item not found".
"""

import asyncio
import logging
import math

import httpx2

logger = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Methods whose effect is the same when applied twice, so a 5xx may be replayed.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "PATCH", "DELETE"})

MAX_ATTEMPTS = 3
BASE_BACKOFF = 0.5
MAX_DELAY = 10.0
# Request extension holding the event-loop time the caller gives up at. A retry
# whose wait would reach it is not attempted: the response is returned instead.
DEADLINE_EXTENSION = "glpi_deadline"
# Request extension marking a POST that changes nothing (a GraphQL query), so a
# 5xx may be replayed as for an idempotent method.
READ_ONLY_EXTENSION = "glpi_read_only"


def parse_retry_after(raw: str | None) -> float | None:
    """Parse a Retry-After header value into seconds.

    Args:
        raw: The header value, if any.

    Returns:
        float | None: Delay in seconds, or None if absent or unparsable.
    """
    if not raw:
        return None
    try:
        # Only the delta-seconds form is handled; the HTTP-date form is rare
        # here and not worth the parsing risk.
        delay = float(raw.strip())
    except ValueError:
        return None
    # float() also accepts "inf" and "nan", which no delay can use.
    return delay if math.isfinite(delay) and delay >= 0 else None


class RetryTransport(httpx2.AsyncBaseTransport):
    """Transport wrapper that retries GLPI requests on transient failures."""

    def __init__(
        self,
        wrapped: httpx2.AsyncBaseTransport,
        *,
        owns_wrapped: bool,
        max_attempts: int = MAX_ATTEMPTS,
    ):
        """Wrap a transport.

        Args:
            wrapped: The transport that actually sends requests.
            owns_wrapped: Close ``wrapped`` when this transport is closed. False
                for a transport shared between clients, which must outlive them.
            max_attempts: Attempts per request; 1 sends each request once.
        """
        self._wrapped = wrapped
        self._owns_wrapped = owns_wrapped
        self._max_attempts = max_attempts

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        """Send a request, retrying it when the server reports a transient failure.

        Args:
            request: The outgoing request, replayed unchanged on each attempt.

        Returns:
            httpx2.Response: The first non-retryable response, or the last attempt.
        """
        max_attempts = self._max_attempts
        loop = asyncio.get_running_loop()
        deadline = request.extensions.get(DEADLINE_EXTENSION)
        replayable = request.method in IDEMPOTENT_METHODS or bool(
            request.extensions.get(READ_ONLY_EXTENSION)
        )
        # A 5xx answered an earlier attempt, so GLPI may have acted on it.
        earlier_5xx = False
        for attempt in range(1, max_attempts + 1):
            started = loop.time()
            response = await self._wrapped.handle_async_request(request)
            if (
                earlier_5xx
                and request.method == "DELETE"
                and response.status_code == 404
            ):
                await response.aclose()
                logger.warning(
                    "DELETE %s answered 404 after a server error: an earlier "
                    "attempt deleted it, reporting success",
                    request.url.path,
                )
                return httpx2.Response(204, request=request)
            if response.status_code not in RETRY_STATUSES or attempt == max_attempts:
                return response
            if response.status_code != 429 and not replayable:
                return response

            delay = parse_retry_after(response.headers.get("Retry-After"))
            if delay is None:
                delay = min(BASE_BACKOFF * 2 ** (attempt - 1), MAX_DELAY)
            elif delay > MAX_DELAY:
                # Retrying sooner than the server asked would only be refused
                # again; hand the response (and its Retry-After) to the caller.
                return response
            now = loop.time()
            # The retry needs about as long as this attempt took, on top of the
            # wait; sending it with too little time left would cut it off mid-way.
            if deadline is not None and now + delay + (now - started) >= deadline:
                # The caller's timeout would cut the retry short and lose GLPI's
                # answer (a refused write would look like one that may have
                # gone through), so hand that answer over now.
                return response
            earlier_5xx = earlier_5xx or response.status_code >= 500
            await response.aclose()

            logger.warning(
                "GLPI API returned HTTP %d (attempt %d/%d), retrying in %.1fs",
                response.status_code,
                attempt,
                max_attempts,
                delay,
            )
            await asyncio.sleep(delay)

        raise RuntimeError("unreachable: retry loop always returns")

    async def aclose(self) -> None:
        if self._owns_wrapped:
            await self._wrapped.aclose()


# One network transport per TLS setting for the whole process. Building one
# creates an SSL context (loading the trust store) and a connection pool, so
# doing it per tool call would add a fresh TLS handshake to every call.
_shared_transports: dict[bool, httpx2.AsyncHTTPTransport] = {}


def shared_transport(verify_ssl: bool) -> httpx2.AsyncHTTPTransport:
    """Return the process-wide network transport for a TLS setting.

    With ``verify_ssl`` on, certificates are checked against the operating
    system trust store (via truststore), so a GLPI behind an internal CA works
    once that CA is trusted by the OS. ``SSL_CERT_FILE``/``SSL_CERT_DIR`` still
    take precedence when set.

    The pooled connections belong to the event loop that opened them, which is
    fine for the server's single loop.

    Args:
        verify_ssl: Whether to validate the server's TLS certificate.

    Returns:
        httpx2.AsyncHTTPTransport: A transport shared by every GLPI client.
    """
    transport = _shared_transports.get(verify_ssl)
    if transport is None:
        transport = httpx2.AsyncHTTPTransport(verify=verify_ssl)
        _shared_transports[verify_ssl] = transport
    return transport


def build_client(
    base_url: str,
    timeout: float,
    headers: dict[str, str],
    transport: httpx2.AsyncBaseTransport,
    *,
    retry: bool = True,
) -> httpx2.AsyncClient:
    """Create the HTTP client used for one GLPI API connection.

    Redirects are followed - a GLPI_URL that redirects (http:// to https://, a
    missing path prefix) would otherwise return the redirect page as data.
    httpx2 drops the Authorization header when a redirect leaves the origin.

    Args:
        base_url: URL every request path is relative to.
        timeout: Per-operation timeout in seconds (connect, read, write, pool).
        headers: Default headers (authorization, accept) for every request.
        transport: Transport that sends requests. It is not closed with the
            client, so it can be shared.
        retry: Retry transient failures. Off for callers that must answer fast
            and pass GLPI's Retry-After on instead of waiting it out.

    Returns:
        httpx2.AsyncClient: Client with retries installed unless ``retry`` is off.
    """
    # Always wrapped, even without retries: AsyncClient.aclose() closes its
    # transport, and the wrapper keeps that from closing a shared one (and
    # every connection other clients have in flight on it).
    transport = RetryTransport(
        transport, owns_wrapped=False, max_attempts=MAX_ATTEMPTS if retry else 1
    )
    return httpx2.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
        transport=transport,
    )
