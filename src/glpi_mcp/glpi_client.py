import asyncio
import hashlib
import logging
import os
import re
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from typing import ClassVar
from urllib.parse import unquote

import httpx2
from fastmcp.server.dependencies import get_access_token

from glpi_mcp.models import SUPPORTED_API_VERSIONS
from glpi_mcp.models import GlpiConfig
from glpi_mcp.models import TransportConfig
from glpi_mcp.retry import DEADLINE_EXTENSION
from glpi_mcp.retry import READ_ONLY_EXTENSION
from glpi_mcp.retry import build_client
from glpi_mcp.retry import parse_retry_after
from glpi_mcp.retry import shared_transport
from glpi_mcp.utils import HTTP_TRANSPORTS
from glpi_mcp.utils import env_bool
from glpi_mcp.utils import env_float
from glpi_mcp.utils import env_int
from glpi_mcp.utils import env_str
from glpi_mcp.utils import http_host_from_env
from glpi_mcp.utils import http_port_from_env
from glpi_mcp.utils import transport_from_env

logger = logging.getLogger(__name__)

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Failures raised before any byte of the request reached GLPI: always safe to
# retry, even for a write.
PRE_SEND_ERRORS = (
    httpx2.ConnectError,
    httpx2.ConnectTimeout,
    httpx2.PoolTimeout,
    httpx2.UnsupportedProtocol,
)
# The total in "start-end/total"; GLPI sends "0--1/0" when nothing matches.
CONTENT_RANGE = re.compile(r"/(\d+)\s*$")
# Seconds before its expiry that a password-grant token is replaced, so a call
# never starts with a token that runs out while it is in flight.
TOKEN_RENEW_MARGIN = 60

# Event-loop time at which the request being served gives up. Set once per
# request so the version lookup, token grant, request and retries share one
# GLPI_TIMEOUT budget instead of getting one each.
_CALL_DEADLINE: ContextVar[float | None] = ContextVar(
    "glpi_call_deadline", default=None
)


def token_fingerprint(token: str) -> str:
    """Identify a token without keeping it: its SHA-256 hex digest."""
    return hashlib.sha256(token.encode()).hexdigest()


def bounded_put(cache: dict[Any, Any], key: Any, value: Any, limit: int) -> None:
    """Store ``key`` as the newest entry, dropping the oldest past ``limit``."""
    cache.pop(key, None)
    cache[key] = value
    while len(cache) > limit:
        del cache[next(iter(cache))]


def is_token_rejection(status: int, body: Any) -> bool:
    """Whether GLPI refused the request's access token itself.

    GLPI answers 400 ERROR_INVALID_PARAMETER "Invalid OAuth token" for a
    malformed, expired or revoked token, not 401, so the title tells it apart
    from an ordinary bad parameter.
    """
    if status == 401:
        return True
    if status != 400 or not isinstance(body, dict):
        return False
    return "oauth token" in str(body.get("title", "")).lower()


class MissingTokenError(Exception):
    """Token passthrough is on but the current request carries no GLPI token."""


class GlpiVersionError(Exception):
    """GLPI does not offer a High-Level API version this server can use."""


class GlpiAPIError(Exception):
    """An HTTP error response from GLPI, keeping its body.

    The High-Level API explains a failure as ``{"status": "ERROR_...", "title":
    ..., "detail": ..., "additional_messages": [...]}``, the token endpoint as
    ``{"error": ..., "error_description": ..., "hint": ...}``. A bare status
    code tells the caller nothing it can act on, so the body is carried through
    to the tool. ``retry_after`` keeps GLPI's Retry-After (seconds), e.g. on a 429.
    """

    def __init__(
        self,
        status: int,
        method: str,
        path: str,
        body: Any,
        retry_after: float | None = None,
    ):
        self.status = status
        self.method = method
        self.path = path
        self.body = body
        self.retry_after = retry_after
        super().__init__(f"{method} {path} returned HTTP {status}: {body}")

    @property
    def token_rejected(self) -> bool:
        """GLPI refused the access token (see ``is_token_rejection``)."""
        return is_token_rejection(self.status, self.body)

    @property
    def error_code(self) -> str | None:
        """GLPI's error code (``ERROR_ITEM_NOT_FOUND``, ``invalid_grant``...)."""
        if isinstance(self.body, dict):
            code = self.body.get("status") or self.body.get("error")
            return str(code) if code else None
        return None


# Failures of an optional lookup (version, OpenAPI document) that must degrade
# to a fallback instead of failing the tool call.
LOOKUP_ERRORS = (GlpiAPIError, httpx2.HTTPError, httpx2.InvalidURL, TimeoutError)


class ResponseTooLargeError(ValueError):
    """A response body is larger than the caller allowed; it was not read."""

    def __init__(self, size: int | None, limit: int):
        self.size = size
        self.limit = limit
        shown = f"is {size} bytes" if size is not None else "has more bytes than that"
        super().__init__(f"The file {shown}, more than max_bytes={limit}.")


class AmbiguousWriteError(Exception):
    """A write whose request was sent but whose outcome is unknown.

    A timeout or dropped connection after the request left may still have been
    committed by GLPI. Retrying blindly can create a duplicate, so the caller
    is told to check first.
    """


@dataclass
class Page:
    """One page of a GLPI collection."""

    rows: list[Any]
    total: int | None
    start: int
    limit: int

    @property
    def has_more(self) -> bool:
        if self.total is not None:
            return self.start + len(self.rows) < self.total
        # Without a Content-Range, a full page is the only hint of more rows.
        return len(self.rows) >= self.limit


@dataclass
class Download:
    """A file downloaded from GLPI."""

    content: bytes
    content_type: str
    filename: str | None


def parse_content_range(raw: str | None) -> int | None:
    """Read the total from a ``Content-Range: start-end/total`` header."""
    if not raw:
        return None
    match = CONTENT_RANGE.search(raw)
    return int(match.group(1)) if match else None


def parse_filename(disposition: str) -> str | None:
    """Read the file name from a Content-Disposition header.

    The RFC 5987 ``filename*=charset''percent-encoded`` form wins over a plain
    ``filename=``, as browsers do.
    """
    extended = re.search(
        r"filename\*\s*=\s*([\w!#$%&+^`{}~-]+)'[^']*'([^;\s]+)",
        disposition,
        re.IGNORECASE,
    )
    if extended:
        try:
            return unquote(extended.group(2), encoding=extended.group(1))
        except LookupError:
            return unquote(extended.group(2))
    plain = re.search(
        r'filename\s*=\s*(?:"([^"]*)"|([^;\s]+))', disposition, re.IGNORECASE
    )
    if plain:
        return plain.group(1) if plain.group(1) is not None else plain.group(2)
    return None


def pick_api_version(offered: list[dict[str, Any]], requested: str) -> str:
    """Choose the High-Level API version to pin in every request path.

    GLPI silently routes an unknown version to its newest one, so the version
    must be one it lists - never a guess.

    Args:
        offered: ``api_versions`` from ``GET /api.php/``.
        requested: ``"auto"`` or a version from ``SUPPORTED_API_VERSIONS``.

    Returns:
        str: ``"major.minor"``, e.g. ``"2.3"``.

    Raises:
        GlpiVersionError: If GLPI offers none of the usable versions.
    """
    available = set()
    for entry in offered:
        if not isinstance(entry, dict):
            continue
        version = str(entry.get("version", ""))
        match = re.match(r"(\d+)\.(\d+)", version)
        if match:
            available.add(f"{match.group(1)}.{match.group(2)}")
    candidates = SUPPORTED_API_VERSIONS if requested == "auto" else (requested,)
    for candidate in candidates:
        if candidate in available:
            return candidate
    offered_text = ", ".join(sorted(available)) or "none"
    if requested == "auto":
        raise GlpiVersionError(
            f"GLPI offers High-Level API versions {offered_text}, but this server "
            f"needs one of {', '.join(SUPPORTED_API_VERSIONS)} (GLPI 11.0.6 or newer)."
        )
    raise GlpiVersionError(
        f"GLPI_API_VERSION={requested} is not offered by this GLPI (it offers "
        f"{offered_text}). Set GLPI_API_VERSION=auto or a listed version."
    )


class GlpiClient:
    """Async client for the GLPI High-Level API (``/api.php``).

    Used as ``async with GlpiClient(config) as glpi``. Each context gets a light
    HTTP client over a transport shared by the whole process, so connections
    and the TLS context are reused across tool calls. Lookups that are stable
    for the life of the process - the API versions GLPI offers, its OpenAPI
    document and the password-grant token - are cached on the class so they
    cost one request per server, not one per tool call.
    """

    _versions_cache: ClassVar[dict[str, list[dict[str, Any]]]] = {}
    _openapi_cache: ClassVar[dict[tuple[str, str], dict[str, Any]]] = {}
    # (url, client_id, username) -> (access token, monotonic expiry).
    _token_cache: ClassVar[dict[tuple[str, str, str], tuple[str, float]]] = {}
    _token_locks: ClassVar[dict[tuple[str, str, str], asyncio.Lock]] = {}
    _redirect_warned: ClassVar[set[str]] = set()

    def __init__(
        self,
        config: GlpiConfig,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        token: str | None = None,
        retry: bool = True,
    ):
        """Initialize the client.

        Args:
            config: GLPI connection settings.
            transport: Transport to send requests through. Defaults to the
                process-wide network transport for ``config.verify_ssl``. It
                is never closed by this client.
            token: GLPI OAuth access token to use instead of the password grant.
            retry: Retry 429 and 5xx answers. Off for the token check, which
                must fail fast and pass GLPI's Retry-After on to the client.
        """
        self.config = config
        self._token = token
        self._retry = retry
        self.base_url = config.glpi_url.rstrip("/")
        self.api_root = f"{self.base_url}/api.php"
        self._transport = transport
        self._http: httpx2.AsyncClient | None = None
        # Token of the caller (explicit or passed through), or None to use the
        # server's own password-grant token.
        self._caller_token: str | None = None

    @classmethod
    def reset_cache(cls) -> None:
        """Forget cached versions, documents and tokens (used by tests)."""
        cls._versions_cache.clear()
        cls._openapi_cache.clear()
        cls._token_cache.clear()
        cls._token_locks.clear()
        cls._redirect_warned.clear()

    def _resolve_caller_token(self) -> str | None:
        """Return the caller's GLPI token, or None to use the password grant.

        With token passthrough the token comes from the MCP request being
        served, never from the server's credentials: a request without one
        must not borrow the server's identity.

        Raises:
            MissingTokenError: If an explicit token is empty, or passthrough is
                on and the request has no token.
        """
        if self._token is not None:
            if not self._token:
                raise MissingTokenError("An empty GLPI token was given.")
            return self._token
        if self.config.per_user_tokens:
            access_token = get_access_token()
            if access_token is None or not access_token.token:
                raise MissingTokenError(
                    "No GLPI token on this request. Sign in with GLPI or send "
                    "your GLPI OAuth access token as 'Authorization: Bearer <token>'."
                    if self.config.token_passthrough
                    else "No GLPI login on this request. Sign in with GLPI first."
                )
            return access_token.token
        return None

    def _scope_headers(self) -> dict[str, str]:
        """Entity and profile headers GLPI applies to the request's session."""
        headers: dict[str, str] = {}
        if self.config.profile_id is not None:
            headers["GLPI-Profile"] = str(self.config.profile_id)
        if self.config.entity_id is not None:
            headers["GLPI-Entity"] = str(self.config.entity_id)
            if self.config.entity_recursive:
                headers["GLPI-Entity-Recursive"] = "true"
        return headers

    async def __aenter__(self) -> "GlpiClient":
        self._caller_token = self._resolve_caller_token()
        self._http = build_client(
            self.api_root,
            self.config.timeout,
            {"Accept": "application/json", **self._scope_headers()},
            self._transport or shared_transport(self.config.verify_ssl),
            retry=self._retry,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        return False

    # ------------------------------------------------------------------ auth

    @property
    def _token_key(self) -> tuple[str, str, str]:
        return (
            self.base_url,
            self.config.client_id or "",
            self.config.username or "",
        )

    async def _access_token(self, *, refused: str | None = None) -> str:
        """Return the bearer token for this context.

        Args:
            refused: A password-grant token GLPI answered 401 to. It is
                replaced unless a concurrent call already did so.

        Raises:
            GlpiAPIError: If GLPI refuses the password grant.
        """
        if self._caller_token is not None:
            return self._caller_token
        key = self._token_key
        cached = self._token_cache.get(key)
        if cached and cached[1] > time.monotonic() and cached[0] != refused:
            return cached[0]
        lock = self._token_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._token_locks[key] = lock
        async with lock:
            current = self._token_cache.get(key)
            # Checked again under the lock: a concurrent call may already have
            # fetched (or, after a 401, replaced) the token while this one
            # waited. Only the token this call saw refused is replaced.
            if current and current[1] > time.monotonic() and current[0] != refused:
                return current[0]
            token, expires_in = await self._password_grant()
            lifetime = max(expires_in - TOKEN_RENEW_MARGIN, expires_in / 2)
            self._token_cache[key] = (token, time.monotonic() + lifetime)
            return token

    async def _password_grant(self) -> tuple[str, float]:
        """Obtain an access token with the OAuth password grant.

        Returns:
            tuple[str, float]: The access token and its lifetime in seconds.
        """
        missing = self.config.missing_credentials()
        if missing:
            raise MissingTokenError(
                f"GLPI credentials are not configured: {', '.join(missing)}"
            )
        if self._http is None:
            raise RuntimeError("GlpiClient must be used as an async context manager")
        form = {
            "grant_type": "password",
            "client_id": self.config.client_id or "",
            "client_secret": self.config.client_secret or "",
            "username": self.config.username or "",
            "password": self.config.password or "",
            "scope": " ".join(self.config.scopes),
        }
        try:
            async with self._deadline() as deadline:
                response = await self._http.post(
                    "/token", data=form, extensions=self._deadline_extensions(deadline)
                )
        except TimeoutError as exc:
            raise TimeoutError(
                f"GLPI did not issue a token within {self.config.timeout}s"
            ) from exc
        if response.history:
            self._check_redirect("POST", "token", response)
        body = self._decode(response)
        if response.status_code >= 400:
            raise GlpiAPIError(
                response.status_code,
                "POST",
                "token",
                body or response.reason_phrase,
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
            )
        if not isinstance(body, dict) or not body.get("access_token"):
            raise GlpiAPIError(
                response.status_code, "POST", "token", "No access_token in response"
            )
        expires_in = body.get("expires_in")
        lifetime = float(expires_in) if isinstance(expires_in, int | float) else 3600
        logger.debug("Obtained a GLPI access token valid for %ss", lifetime)
        return str(body["access_token"]), lifetime

    # ------------------------------------------------------------------ core

    @staticmethod
    def _decode(response: httpx2.Response) -> Any:
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text[:2000]

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        accept: str | None = None,
        max_bytes: int | None = None,
        read_only: bool = False,
    ) -> httpx2.Response:
        """Send one authenticated request and return the successful response.

        A 401 to the server's own password-grant token renews the token and
        sends the request once more: GLPI may have revoked or expired it early.

        ``read_only`` marks a POST that changes nothing (a GraphQL query), so a
        lost answer is reported as a timeout, not as an ambiguous write.

        Raises:
            GlpiAPIError: On any HTTP error status.
            AmbiguousWriteError: When a write was sent but its outcome is unknown.
            TimeoutError: When a read does not finish within ``config.timeout``.
            ValueError: When a redirect turned a write into a GET.
            ResponseTooLargeError: When a successful body exceeds ``max_bytes``.
        """
        if self._http is None:
            raise RuntimeError("GlpiClient must be used as an async context manager")
        # Set once the request itself has gone out; a timeout after that may
        # have left a write applied.
        sent: list[bool] = []
        try:
            # One GLPI_TIMEOUT budget for the version lookup, token grant, the
            # request and its retries together.
            async with self._deadline():
                return await self._send_with_token(
                    method, path, params, json, accept, max_bytes, read_only, sent
                )
        except TimeoutError as exc:
            if sent and method in WRITE_METHODS and not read_only:
                raise AmbiguousWriteError(
                    f"{method} {path} was sent but no response arrived within "
                    f"{self.config.timeout}s. The change MAY have been applied - "
                    "check the current state in GLPI before retrying."
                ) from exc
            raise TimeoutError(
                f"GLPI did not answer {method} {path} within {self.config.timeout}s"
            ) from exc

    async def _send_with_token(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        json: Any,
        accept: str | None,
        max_bytes: int | None,
        read_only: bool,
        sent: list[bool],
    ) -> httpx2.Response:
        """Body of ``_send``, run inside the request's deadline."""

        url = f"/v{await self.api_version()}/{path.strip('/')}"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        refused: str | None = None
        while True:
            token = await self._access_token(refused=refused)
            headers = {"Authorization": f"Bearer {token}"}
            if accept:
                headers["Accept"] = accept
            sent.append(True)
            response = await self._send_once(
                method, path, url, clean_params, json, headers, max_bytes, read_only
            )
            if (
                self._caller_token is None
                and refused is None
                # Checked first so a successful body is not decoded twice.
                and response.status_code in (400, 401)
                and is_token_rejection(response.status_code, self._decode(response))
            ):
                refused = token
                # GLPI refused the request without acting on it, so a timeout
                # while a new token is fetched must not read as an ambiguous write.
                sent.clear()
                logger.info("GLPI refused the access token, requesting a new one")
                continue
            break

        if response.history:
            self._check_redirect(method, path, response)
        if response.status_code >= 400:
            raise GlpiAPIError(
                response.status_code,
                method,
                path,
                self._decode(response) or response.reason_phrase,
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
            )
        return response

    @asynccontextmanager
    async def _deadline(self) -> AsyncGenerator[float]:
        """Bound everything inside to the current request's GLPI_TIMEOUT budget.

        The outermost scope starts the budget; nested scopes (the version
        lookup and token grant a request triggers) share it, so one tool request
        never waits longer than GLPI_TIMEOUT in total.
        """
        deadline = _CALL_DEADLINE.get()
        if deadline is not None:
            # Only the outermost scope arms the timeout: nested timeouts with
            # the same deadline would race to fire first.
            yield deadline
            return
        deadline = asyncio.get_running_loop().time() + self.config.timeout
        reset = _CALL_DEADLINE.set(deadline)
        try:
            async with asyncio.timeout_at(deadline):
                yield deadline
        finally:
            _CALL_DEADLINE.reset(reset)

    @staticmethod
    def _deadline_extensions(deadline: float) -> dict[str, Any]:
        """Request extensions telling the retrying transport when the call gives up."""
        return {DEADLINE_EXTENSION: deadline}

    async def _send_once(
        self,
        method: str,
        path: str,
        url: str,
        params: dict[str, Any],
        json: Any,
        headers: dict[str, str],
        max_bytes: int | None = None,
        read_only: bool = False,
    ) -> httpx2.Response:
        """Send the request once, translating transport failures.

        With ``max_bytes``, a successful body is read only up to that size, so
        a huge file is refused without being buffered in memory.
        """
        if self._http is None:
            raise RuntimeError("GlpiClient must be used as an async context manager")
        try:
            # httpx2's timeout applies to each connect/read/write step; the
            # deadline caps the whole call, retries included, as GLPI_TIMEOUT
            # promises.
            async with self._deadline() as deadline:
                # Lets the retrying transport hand back a 429/5xx instead of
                # sleeping past the deadline, which would turn a refused write
                # into an ambiguous one.
                extensions: dict[str, Any] = self._deadline_extensions(deadline)
                if read_only:
                    # A 5xx to a query that changes nothing may be replayed.
                    extensions[READ_ONLY_EXTENSION] = True
                if max_bytes is None:
                    return await self._http.request(
                        method,
                        url,
                        params=params or None,
                        json=json,
                        headers=headers,
                        extensions=extensions,
                    )
                request = self._http.build_request(
                    method,
                    url,
                    params=params or None,
                    json=json,
                    headers=headers,
                    extensions=extensions,
                )
                response = await self._http.send(request, stream=True)
                try:
                    if response.status_code >= 400:
                        await response.aread()
                        return response
                    return await self._read_bounded(response, max_bytes)
                finally:
                    await response.aclose()
        except PRE_SEND_ERRORS:
            # Nothing reached GLPI, so a retry is safe.
            raise
        except httpx2.RequestError as exc:
            # The overall GLPI_TIMEOUT is mapped in _send, which owns the
            # deadline; this handles a connection lost after sending.
            if method in WRITE_METHODS and not read_only:
                raise AmbiguousWriteError(
                    f"{method} {path} was sent but no valid response arrived "
                    f"({type(exc).__name__}: {exc}). The change MAY have been "
                    "applied - check the current state in GLPI before retrying."
                ) from exc
            raise

    @staticmethod
    async def _read_bounded(
        response: httpx2.Response, max_bytes: int
    ) -> httpx2.Response:
        """Read a streamed body, refusing it once it passes ``max_bytes``.

        Raises:
            ResponseTooLargeError: If the body is larger than ``max_bytes``.
        """
        length = response.headers.get("Content-Length", "")
        if length.isdigit() and int(length) > max_bytes:
            raise ResponseTooLargeError(int(length), max_bytes)
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise ResponseTooLargeError(None, max_bytes)
            chunks.append(chunk)
        # The chunks are already decoded, so the encoding headers no longer apply.
        headers = [
            (name, value)
            for name, value in response.headers.multi_items()
            if name.lower() not in ("content-encoding", "content-length")
        ]
        return httpx2.Response(
            response.status_code,
            headers=headers,
            content=b"".join(chunks),
            request=response.request,
            history=response.history,
        )

    def _check_redirect(
        self, method: str, path: str, response: httpx2.Response
    ) -> None:
        """Reject a write a redirect turned into a GET, and flag GLPI_URL once.

        A 301/302/303 answer to a POST/PATCH/DELETE is followed as a GET, so the
        write never happened even though the final response succeeds.

        Raises:
            ValueError: If the method changed along the redirect chain.
        """
        final_url = response.request.url
        if response.request.method != method:
            raise ValueError(
                f"{method} {path} was redirected to {final_url} and turned into a "
                f"{response.request.method}, so the change was NOT applied. Set "
                "GLPI_URL to the final address (e.g. https:// instead of http://)."
            )
        if self.base_url not in self._redirect_warned:
            self._redirect_warned.add(self.base_url)
            logger.warning(
                "GLPI redirects %s to %s - set GLPI_URL to the final address "
                "to avoid an extra round trip on every call.",
                response.history[0].request.url,
                final_url,
            )

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        read_only: bool = False,
    ) -> Any:
        """Send one request to the pinned API version and decode its JSON body.

        Args:
            method: HTTP method.
            path: Endpoint relative to the version root, e.g. ``"Assistance/Ticket"``.
            params: Query parameters; ``None`` values are dropped.
            json: JSON request body.
            read_only: The request changes nothing even though it is a POST.

        Returns:
            Any: Decoded JSON, or ``None`` for an empty successful response.
        """
        response = await self._send(
            method, path, params=params, json=json, read_only=read_only
        )
        return self._decode(response)

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, json: Any, **kwargs: Any) -> Any:
        return await self.request("POST", path, json=json, **kwargs)

    async def patch(self, path: str, json: Any, **kwargs: Any) -> Any:
        return await self.request("PATCH", path, json=json, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> Any:
        return await self.request("DELETE", path, **kwargs)

    async def collection(
        self,
        path: str,
        *,
        filter: str | None = None,
        sort: str | None = None,
        start: int = 0,
        limit: int = 20,
    ) -> Page:
        """Fetch one page of a collection endpoint.

        Args:
            path: Collection endpoint, e.g. ``"Assistance/Ticket"``.
            filter: RSQL filter.
            sort: ``"field:asc,other:desc"``.
            start: Index of the first row.
            limit: Page size.

        Returns:
            Page: The rows with the total GLPI reports in Content-Range.
        """
        query = {
            "filter": filter or None,
            "sort": sort or None,
            "start": start,
            "limit": limit,
        }
        response = await self._send("GET", path, params=query)
        body = self._decode(response)
        rows = body if isinstance(body, list) else ([] if body is None else [body])
        total = parse_content_range(response.headers.get("Content-Range"))
        return Page(rows=rows, total=total, start=start, limit=limit)

    async def download(self, path: str, max_bytes: int | None = None) -> Download:
        """Download a file endpoint, e.g. ``Management/Document/5/Download``.

        Raises:
            ResponseTooLargeError: If the file is larger than ``max_bytes``.
        """
        response = await self._send("GET", path, accept="*/*", max_bytes=max_bytes)
        return Download(
            content=response.content,
            content_type=response.headers.get(
                "Content-Type", "application/octet-stream"
            ),
            filename=parse_filename(response.headers.get("Content-Disposition", "")),
        )

    # ------------------------------------------------------------ discovery

    async def api_versions(self) -> list[dict[str, Any]]:
        """Return the API versions GLPI lists at ``GET /api.php/``, cached per URL.

        Raises:
            GlpiVersionError: If GLPI has no High-Level API (GLPI 10 or older,
                or a GLPI_URL that does not point at GLPI).
        """
        cached = self._versions_cache.get(self.base_url)
        if cached is not None:
            return cached
        if self._http is None:
            raise RuntimeError("GlpiClient must be used as an async context manager")
        try:
            async with self._deadline() as deadline:
                # The deadline keeps a retry from sleeping past the timeout and
                # losing GLPI's own answer (a 503 with Retry-After, say).
                response = await self._http.get(
                    "/", extensions=self._deadline_extensions(deadline)
                )
        except TimeoutError as exc:
            raise TimeoutError(
                f"GLPI did not list its API versions within {self.config.timeout}s"
            ) from exc
        body = self._decode(response)
        if response.status_code == 404 or (
            response.status_code < 400
            and not (isinstance(body, dict) and "api_versions" in body)
        ):
            raise GlpiVersionError(
                f"{self.api_root}/ does not list High-Level API versions. This "
                "server needs GLPI 11.0.6 or newer; check GLPI_URL (the GLPI root, "
                "without /api.php) and that the API is enabled in Setup > General > API."
            )
        if response.status_code >= 400:
            raise GlpiAPIError(
                response.status_code,
                "GET",
                "/",
                body or response.reason_phrase,
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
            )
        versions = [v for v in body["api_versions"] if isinstance(v, dict)]
        self._versions_cache[self.base_url] = versions
        return versions

    async def api_version(self) -> str:
        """Return the High-Level API version pinned in every request path."""
        return pick_api_version(await self.api_versions(), self.config.api_version)

    async def openapi(self) -> dict[str, Any]:
        """Return GLPI's OpenAPI document for the pinned version, cached."""
        version = await self.api_version()
        key = (self.base_url, version)
        cached = self._openapi_cache.get(key)
        if cached is None:
            cached = await self.get("doc.json")
            if not isinstance(cached, dict):
                raise GlpiAPIError(200, "GET", "doc.json", "Not an OpenAPI document")
            self._openapi_cache[key] = cached
        return cached


def _env_setting(name: str, field: str) -> int:
    """Read an integer setting, defaulting to GlpiConfig's own default."""
    return env_int(name, GlpiConfig.model_fields[field].default)


def _env_raw(name: str) -> str | None:
    """Read a secret unstripped, as spaces may be part of it; blank is unset."""
    raw = os.getenv(name)
    return raw if raw and raw.strip() else None


def _csv(name: str) -> list[str] | None:
    """Read a comma- or space-separated list; blank is unset."""
    raw = env_str(name)
    if raw is None:
        return None
    return [item for item in re.split(r"[,\s]+", raw) if item]


def _optional_int(name: str) -> int | None:
    return env_int(name, 0) if env_str(name) else None


def get_glpi_config_from_env() -> GlpiConfig:
    """Get GLPI configuration from environment variables."""
    disabled_tags = set(_csv("DISABLED_TAGS") or [])

    # Blank means unlimited; zero or less is rejected by GlpiConfig.
    response_max_size = _optional_int("RESPONSE_MAX_SIZE")

    rate_limit_enabled = env_bool("RATE_LIMIT_ENABLED", default=False)
    tool_search_enabled = env_bool("TOOL_SEARCH_ENABLED", default=False)

    # Settings of a disabled feature are not read at all: a leftover value must
    # not stop a server that never uses it, and GlpiConfig's own defaults then
    # apply. An unknown TOOL_SEARCH_STRATEGY is refused once search is on.
    features: dict[str, Any] = {}
    if rate_limit_enabled:
        features["rate_limit_max_requests"] = _env_setting(
            "RATE_LIMIT_MAX_REQUESTS", "rate_limit_max_requests"
        )
        features["rate_limit_window_minutes"] = _env_setting(
            "RATE_LIMIT_WINDOW_MINUTES", "rate_limit_window_minutes"
        )
    if tool_search_enabled:
        strategy = (env_str("TOOL_SEARCH_STRATEGY") or "bm25").lower()
        if strategy not in ("bm25", "regex"):
            raise ValueError(
                "TOOL_SEARCH_STRATEGY must be 'bm25' or 'regex', "
                f"got {os.getenv('TOOL_SEARCH_STRATEGY')!r}"
            )
        features["tool_search_strategy"] = strategy
        features["tool_search_max_results"] = _env_setting(
            "TOOL_SEARCH_MAX_RESULTS", "tool_search_max_results"
        )

    scopes = _csv("GLPI_SCOPES")
    if scopes is not None:
        features["scopes"] = scopes

    api_version = (env_str("GLPI_API_VERSION") or "auto").lower()
    if api_version not in ("auto", *SUPPORTED_API_VERSIONS):
        raise ValueError(
            "GLPI_API_VERSION must be 'auto' or one of "
            f"{', '.join(SUPPORTED_API_VERSIONS)}, got {os.getenv('GLPI_API_VERSION')!r}"
        )

    return GlpiConfig(
        glpi_url=env_str("GLPI_URL") or "",
        client_id=env_str("GLPI_CLIENT_ID"),
        client_secret=_env_raw("GLPI_CLIENT_SECRET"),
        username=env_str("GLPI_USERNAME"),
        password=_env_raw("GLPI_PASSWORD"),
        api_version=api_version,
        token_passthrough=env_bool("GLPI_TOKEN_PASSTHROUGH", default=False),
        oauth_login=env_bool("GLPI_OAUTH_LOGIN", default=False),
        oauth_base_url=env_str("GLPI_OAUTH_BASE_URL"),
        oauth_redirect_path=env_str("GLPI_OAUTH_REDIRECT_PATH") or "/auth/callback",
        oauth_allowed_redirect_uris=_csv("GLPI_OAUTH_ALLOWED_REDIRECT_URIS"),
        entity_id=_optional_int("GLPI_ENTITY"),
        entity_recursive=env_bool("GLPI_ENTITY_RECURSIVE", default=False),
        profile_id=_optional_int("GLPI_PROFILE"),
        verify_ssl=env_bool("GLPI_VERIFY_SSL", default=True),
        timeout=env_float("GLPI_TIMEOUT", 30),
        read_only_mode=env_bool("READ_ONLY_MODE", default=False),
        dry_run_mode=env_bool("DRY_RUN_MODE", default=False),
        disabled_tags=disabled_tags,
        response_max_size=response_max_size,
        rate_limit_enabled=rate_limit_enabled,
        tool_search_enabled=tool_search_enabled,
        **features,
    )


def get_transport_config_from_env() -> TransportConfig:
    """Get transport configuration from environment variables."""
    transport_type = transport_from_env()
    return TransportConfig(
        transport_type=transport_type,
        http_host=http_host_from_env(),
        # The port is only parsed when an HTTP transport will bind it: a leftover
        # non-numeric MCP_HTTP_PORT must not stop stdio or `fastmcp run`.
        http_port=http_port_from_env() if transport_type in HTTP_TRANSPORTS else 8000,
        http_bearer_token=env_str("MCP_HTTP_BEARER_TOKEN"),
        oidc_config_url=env_str("OIDC_CONFIG_URL"),
        oidc_client_id=env_str("OIDC_CLIENT_ID"),
        oidc_client_secret=_env_raw("OIDC_CLIENT_SECRET"),
        oidc_base_url=env_str("OIDC_BASE_URL"),
        oidc_redirect_path=env_str("OIDC_REDIRECT_PATH") or "/auth/callback",
        oidc_required_scopes=_csv("OIDC_REQUIRED_SCOPES"),
        oidc_allowed_redirect_uris=_csv("OIDC_ALLOWED_REDIRECT_URIS"),
        oidc_verify_id_token=env_bool("OIDC_VERIFY_ID_TOKEN", default=False),
        oidc_forward_resource=env_bool("OIDC_FORWARD_RESOURCE", default=False),
    )
