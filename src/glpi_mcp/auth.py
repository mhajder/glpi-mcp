"""
Authentication for the MCP HTTP transports.

Four mutually exclusive ways for an MCP client to authenticate, in order of
precedence:

1. OIDC (``OIDC_*``): an OAuth interface with Dynamic Client Registration,
   brokered to an external identity provider. Tool calls use the server's own
   GLPI account.
2. GLPI login (``GLPI_OAUTH_LOGIN``): the same OAuth interface, brokered to
   GLPI's own OAuth server, so users sign in with their GLPI account and every
   tool call runs as that user.
3. Token passthrough (``GLPI_TOKEN_PASSTHROUGH``): the client sends a GLPI OAuth
   access token it obtained itself; it is checked against GLPI and forwarded.
4. A static bearer token (``MCP_HTTP_BEARER_TOKEN``).
"""

import asyncio
import logging
import math
import time
from collections import OrderedDict
from contextvars import ContextVar
from typing import Any

import httpx2
from fastmcp.server.auth import AuthProvider
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.auth import TokenVerifier
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.dependencies import get_access_token
from starlette.authentication import AuthenticationError
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.responses import Response

from glpi_mcp.glpi_client import LOOKUP_ERRORS
from glpi_mcp.glpi_client import GlpiAPIError
from glpi_mcp.glpi_client import GlpiClient
from glpi_mcp.glpi_client import GlpiVersionError
from glpi_mcp.glpi_client import bounded_put
from glpi_mcp.glpi_client import token_fingerprint
from glpi_mcp.models import GlpiConfig
from glpi_mcp.models import TransportConfig
from glpi_mcp.utils import HTTP_TRANSPORTS

logger = logging.getLogger(__name__)

# Seconds a client is asked to wait when GLPI cannot verify its token and
# did not say how long to wait; GLPI's own Retry-After is capped at the max.
UNAVAILABLE_RETRY_AFTER = 5
MAX_RETRY_AFTER = 3600


class GlpiUnavailableError(AuthenticationError):
    """GLPI could not say whether a token is valid (outage, 5xx, timeout).

    The message is for the server log only; clients get a generic answer.
    """

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


# Collects the GlpiUnavailableError a token check raised while FastMCP's
# OAuthProxy runs it; the proxy swallows every exception, so this is how the
# outage reaches GlpiOAuthProxy and becomes a 503 instead of a 401.
_UNAVAILABLE: ContextVar[list["GlpiUnavailableError"] | None] = ContextVar(
    "glpi_unavailable", default=None
)


def install_unavailable_handler(middleware: list) -> list:
    """Make FastMCP's auth middleware answer GlpiUnavailableError with a 503.

    Raises:
        RuntimeError: If FastMCP no longer uses Starlette's
            AuthenticationMiddleware, whose default handler would answer 400
            with the internal error message.
    """
    patched = False
    for entry in middleware:
        if entry.cls is AuthenticationMiddleware:
            entry.kwargs["on_error"] = auth_error_response
            patched = True
    if not patched:
        raise RuntimeError(
            "FastMCP's auth middleware changed: cannot install the GLPI "
            "token check's error handler"
        )
    return middleware


def auth_error_response(conn: HTTPConnection, exc: AuthenticationError) -> Response:
    """Answer 503 when GLPI is down, so clients retry instead of re-authenticating.

    The body stays generic: an unauthenticated caller must not learn why the
    server cannot reach GLPI.
    """
    if isinstance(exc, GlpiUnavailableError):
        retry_after = UNAVAILABLE_RETRY_AFTER
        if exc.retry_after is not None:
            retry_after = min(max(math.ceil(exc.retry_after), 1), MAX_RETRY_AFTER)
        return JSONResponse(
            {
                "error": "temporarily_unavailable",
                "error_description": "GLPI could not verify the token; retry later",
            },
            status_code=503,
            headers={"Retry-After": str(retry_after)},
        )
    return AuthenticationMiddleware.default_on_error(conn, exc)


class GlpiTokenVerifier(TokenVerifier):
    """Accept a bearer token only if GLPI accepts it as an OAuth access token.

    The verified token is kept on the request's ``AccessToken``, where
    ``GlpiClient`` picks it up, so every tool call runs as the caller's own
    GLPI user. Accepted tokens are cached for ``ttl`` seconds to save a GLPI
    round trip per MCP message. After that, until ``stale_ttl``, the token is
    still accepted at once while GLPI re-checks it in the background, so a
    known user never waits for, or gets a 503 from, a queued check. A revoked
    token is dropped when that re-check comes back; GLPI refuses it on every
    tool call anyway, since each call carries the token itself.

    Requests are not rate-limited before they authenticate, so the verifier
    shields GLPI itself: a rejected token is remembered for ``reject_ttl``
    seconds, and at most ``max_concurrent_checks`` checks reach GLPI at once.
    """

    def __init__(
        self,
        config: GlpiConfig,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        required_scopes: list[str] | None = None,
        ttl: float = 60,
        stale_ttl: float = 600,
        reject_ttl: float = 30,
        max_entries: int = 1024,
        max_concurrent_checks: int = 8,
        timeout: float = 10,
    ):
        """Initialize the verifier.

        Args:
            config: GLPI connection settings.
            transport: Transport for the verification request. None uses the
                shared network transport; tests pass an httpx2.MockTransport.
            required_scopes: Scopes every accepted token is granted. GLPI does
                not report a token's scopes, so the token is trusted to carry
                the ones the login requested once GLPI has accepted it.
            ttl: Seconds an accepted token is trusted without asking GLPI.
            stale_ttl: Seconds after its last successful check that a token is
                still accepted while being re-checked in the background, e.g.
                through a GLPI outage. Past it, requests wait for a check.
            reject_ttl: Seconds a rejected token is refused without asking GLPI.
            max_entries: Accepted and rejected tokens each kept before the
                oldest is dropped.
            max_concurrent_checks: Token checks sent to GLPI at once; the
                rest queue, and those still queued at ``timeout`` get a 503.
            timeout: Most seconds one token check may take, queueing included
                (capped at ``config.timeout``). Every request with an
                uncached token waits on it, so an outage must answer 503 fast.
                The check is not retried, so GLPI's Retry-After reaches the
                client instead of being slept through.
        """
        super().__init__(required_scopes=required_scopes)
        self.config = config.model_copy(
            update={"timeout": min(config.timeout, timeout)}
        )
        self.transport = transport
        self.ttl = ttl
        self.stale_ttl = max(stale_ttl, ttl)
        self.reject_ttl = reject_ttl
        self.max_entries = max_entries
        # Fingerprint -> (token, fresh until, usable until). The cached
        # AccessToken holds the plaintext token, which tool calls need.
        self._cache: OrderedDict[str, tuple[AccessToken, float, float]] = OrderedDict()
        # Fingerprint -> monotonic time until which the token stays rejected.
        self._rejected: OrderedDict[str, float] = OrderedDict()
        # One GLPI check per token at a time: concurrent requests carrying the
        # same uncached token wait for it instead of each asking GLPI.
        self._pending: dict[str, asyncio.Task[AccessToken | None]] = {}
        self._checks = asyncio.Semaphore(max_concurrent_checks)

    def get_middleware(self) -> list:
        """FastMCP's auth middleware, with a 503 for an unreachable GLPI."""
        return install_unavailable_handler(super().get_middleware())

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return the caller's access token, or None if GLPI rejects it.

        Raises:
            GlpiUnavailableError: If GLPI could not be asked or failed to
                answer, so the token is neither accepted nor rejected.
        """
        token = token.strip()
        # An HTTP header can only carry a printable ASCII token.
        if not token or not token.isascii() or not token.isprintable():
            return None

        key = token_fingerprint(token)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and cached[2] > now:
            access_token, fresh_until, usable_until = cached
            self._cache.move_to_end(key)
            if fresh_until <= now:
                # Stale: answer now and re-check in the background. The entry
                # counts as fresh until the check could have finished, so a
                # failed re-check is retried then, not on every request.
                self._cache[key] = (
                    access_token,
                    now + self.config.timeout,
                    usable_until,
                )
                self._check(token, key)
            return access_token
        self._cache.pop(key, None)
        if self._rejected.get(key, 0) > now:
            return None
        self._rejected.pop(key, None)

        try:
            # Shielded so one cancelled request does not fail the others waiting.
            return await asyncio.shield(self._check(token, key))
        except GlpiUnavailableError as exc:
            sink = _UNAVAILABLE.get()
            if sink is not None:
                sink.append(exc)
            raise

    def _check(self, token: str, key: str) -> asyncio.Task[AccessToken | None]:
        """Return the running GLPI check for ``token``, starting one if needed."""
        task = self._pending.get(key)
        if task is None:
            task = asyncio.create_task(self._ask_glpi(token, key))
            self._pending[key] = task
            task.add_done_callback(lambda done: self._finish_check(key, done))
        return task

    def _finish_check(self, key: str, task: asyncio.Task) -> None:
        """Forget a finished check, marking its error as seen.

        Every waiter may have been cancelled, and an error nobody awaited would
        otherwise be logged by asyncio as "never retrieved".
        """
        self._pending.pop(key, None)
        if not task.cancelled():
            task.exception()

    async def _ask_glpi(self, token: str, key: str) -> AccessToken | None:
        """Ask GLPI whether ``token`` is valid, caching the answer."""
        try:
            # Caps the whole check, queueing included.
            async with (
                asyncio.timeout(self.config.timeout),
                self._checks,
                GlpiClient(
                    self.config, token=token, transport=self.transport, retry=False
                ) as glpi,
            ):
                # /session needs only the 'api' scope and names the user the
                # token belongs to.
                session = await glpi.get("session")
        except GlpiAPIError as exc:
            if exc.status in (401, 403) or exc.token_rejected:
                # Debug only: every bad token would log otherwise. If all tokens
                # are refused, a proxy or redirect may be dropping the header.
                logger.debug(
                    "GLPI rejected a token: HTTP %s (if every token is rejected, "
                    "check that no proxy or redirect drops the Authorization header)",
                    exc.status,
                )
                # A background re-check may find a cached token revoked.
                self._cache.pop(key, None)
                now = time.monotonic()
                # Every entry gets the same TTL and is re-inserted at the end,
                # so the dict is ordered by expiry: drop from the front only.
                while self._rejected and next(iter(self._rejected.values())) <= now:
                    self._rejected.popitem(last=False)
                bounded_put(
                    self._rejected, key, now + self.reject_ttl, limit=self.max_entries
                )
                return None
            # Any other status (5xx, 429, 404 for a wrong GLPI_URL) says nothing
            # about the token, so the client is told to retry, not to re-login.
            logger.warning("GLPI token verification failed: HTTP %s", exc.status)
            raise GlpiUnavailableError(
                f"GLPI could not verify the token (HTTP {exc.status}); retry later",
                retry_after=exc.retry_after,
            ) from exc
        except GlpiVersionError as exc:
            logger.error("GLPI token verification failed: %s", exc)
            raise GlpiUnavailableError(str(exc)) from exc
        except LOOKUP_ERRORS as exc:
            logger.warning("GLPI token verification failed: %s", type(exc).__name__)
            raise GlpiUnavailableError(
                f"GLPI could not verify the token ({type(exc).__name__}); retry later"
            ) from exc

        if not isinstance(session, dict):
            # /session answers a JSON object; anything else (an empty body, a
            # proxy's HTML login page) is not GLPI vouching for the token.
            logger.error(
                "GLPI token verification got a non-JSON-object answer - check GLPI_URL"
            )
            raise GlpiUnavailableError(
                "GLPI could not verify the token (unexpected response); the "
                "server's GLPI_URL may be wrong"
            )
        user_id = session.get("user_id")
        has_id = (
            isinstance(user_id, int) and not isinstance(user_id, bool) and user_id > 0
        )
        access_token = AccessToken(
            token=token,
            # The MCP SDK binds each session to its creator's client_id, so it
            # must tell users apart and stay fixed for the session's life: the
            # user ID, not the login name, which an admin may rename.
            client_id=f"user:{user_id}" if has_id else f"token:{key}",
            scopes=list(self.required_scopes or []),
        )
        now = time.monotonic()
        # Drop unusable entries first: each holds a plaintext token, which should
        # not outlive ``stale_ttl`` just because the token was never sent again.
        for expired in [k for k, entry in self._cache.items() if entry[2] <= now]:
            del self._cache[expired]
        bounded_put(
            self._cache,
            key,
            (access_token, now + self.ttl, now + self.stale_ttl),
            limit=self.max_entries,
        )
        return access_token


def rate_limit_client_id(_context: object) -> str:
    """Rate-limit each authenticated user separately.

    Keyed by the verified ``client_id``: the GLPI user ID with per-user tokens,
    or the MCP client with OIDC. Unauthenticated transports such as stdio share
    one ``"global"`` window.
    """
    access_token = get_access_token()
    if access_token is None or not access_token.token:
        return "global"
    return access_token.client_id


def _check_exclusive(name: str, transport_config: TransportConfig) -> None:
    """Refuse a per-user auth mode next to a setting it would conflict with."""
    if transport_config.transport_type not in HTTP_TRANSPORTS:
        raise ValueError(f"{name} requires MCP_TRANSPORT=http or sse")
    if transport_config.http_bearer_token:
        raise ValueError(
            f"{name} and MCP_HTTP_BEARER_TOKEN are mutually exclusive: both use "
            "the client's Authorization header"
        )
    if transport_config.oidc_enabled:
        raise ValueError(
            f"{name} and OIDC_* are mutually exclusive: OIDC users have no GLPI token"
        )


def glpi_oauth_proxy_class(base: Any) -> Any:
    """Subclass FastMCP's OAuthProxy so a GLPI outage answers 503, not 401.

    OAuthProxy.load_access_token treats any exception from the token check as
    an invalid token, which would send every signed-in user back through the
    browser login during an outage. The subclass notices the outage the check
    reported and raises it, so the auth middleware answers 503 with
    Retry-After and clients simply retry.

    Args:
        base: OAuthProxy (passed in because it is imported lazily).
    """

    class GlpiOAuthProxy(base):
        async def load_access_token(self, token: str) -> Any:
            sink: list[GlpiUnavailableError] = []
            reset = _UNAVAILABLE.set(sink)
            try:
                access_token = await super().load_access_token(token)
            finally:
                _UNAVAILABLE.reset(reset)
            if access_token is None and sink:
                raise sink[-1]
            return access_token

        def get_middleware(self) -> list:
            return install_unavailable_handler(super().get_middleware())

    return GlpiOAuthProxy


def build_glpi_login_provider(
    glpi_config: GlpiConfig, transport: httpx2.AsyncBaseTransport | None = None
) -> AuthProvider:
    """Broker MCP client logins to GLPI's own OAuth server.

    MCP clients register with this server (Dynamic Client Registration) and are
    sent to GLPI's ``/api.php/authorize`` page; the GLPI access token obtained
    for the user is kept by this server, refreshed when it expires, and used
    for that user's tool calls.
    """
    missing = glpi_config.missing_credentials()
    if missing:
        raise ValueError(f"GLPI_OAUTH_LOGIN needs {', '.join(missing)}")
    # Imported lazily: STDIO users should not pay for the import at all.
    from fastmcp.server.auth.oauth_proxy import OAuthProxy

    api_root = f"{glpi_config.glpi_url.rstrip('/')}/api.php"
    logger.info("GLPI OAuth login enabled (authorization server: %s)", api_root)
    return glpi_oauth_proxy_class(OAuthProxy)(
        upstream_authorization_endpoint=f"{api_root}/authorize",
        upstream_token_endpoint=f"{api_root}/token",
        upstream_client_id=glpi_config.client_id or "",
        upstream_client_secret=glpi_config.client_secret,
        token_verifier=GlpiTokenVerifier(
            glpi_config, transport=transport, required_scopes=glpi_config.scopes
        ),
        base_url=glpi_config.oauth_base_url or "",
        redirect_path=glpi_config.oauth_redirect_path,
        allowed_client_redirect_uris=glpi_config.oauth_allowed_redirect_uris,
        valid_scopes=glpi_config.scopes,
        # GLPI does not implement RFC 8707 resource indicators.
        forward_resource=False,
        token_endpoint_auth_method="client_secret_post",  # noqa: S106 - a method name
    )


def build_oidc_provider(transport_config: TransportConfig) -> AuthProvider | None:
    """Broker MCP client logins to an external OIDC identity provider."""
    credentials = transport_config.oidc_credentials()
    if credentials is None:
        return None
    config_url, client_id, client_secret, base_url = credentials

    # Imported lazily: constructing the provider performs OIDC discovery, and
    # STDIO users should not pay for the import at all.
    from fastmcp.server.auth.oidc_proxy import OIDCProxy

    logger.info("OIDC authentication enabled (upstream: %s)", config_url)
    return OIDCProxy(
        config_url=config_url,
        client_id=client_id,
        client_secret=client_secret,
        base_url=base_url,
        redirect_path=transport_config.oidc_redirect_path,
        required_scopes=transport_config.oidc_required_scopes,
        allowed_client_redirect_uris=transport_config.oidc_allowed_redirect_uris,
        verify_id_token=transport_config.oidc_verify_id_token,
        forward_resource=transport_config.oidc_forward_resource,
    )


def build_auth_provider(
    glpi_config: GlpiConfig, transport_config: TransportConfig
) -> AuthProvider | None:
    """Choose how MCP clients authenticate to this server.

    Args:
        glpi_config: GLPI connection settings.
        transport_config: MCP transport settings.

    Returns:
        AuthProvider | None: The provider for the configured mode (see the
            module docstring), or None for no authentication.

    Raises:
        ValueError: If a per-user mode is combined with stdio or another mode,
            or the GLPI login lacks a setting.
    """
    missing = transport_config.missing_oidc_settings()
    if missing:
        logger.warning(
            "OIDC is only partially configured (missing %s) and has been ignored. "
            "Set all of OIDC_CONFIG_URL, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET and "
            "OIDC_BASE_URL to enable it.",
            ", ".join(missing),
        )

    if glpi_config.oauth_login:
        _check_exclusive("GLPI_OAUTH_LOGIN", transport_config)
        return build_glpi_login_provider(glpi_config)

    if glpi_config.token_passthrough:
        _check_exclusive("GLPI_TOKEN_PASSTHROUGH", transport_config)
        return GlpiTokenVerifier(glpi_config)

    oidc = build_oidc_provider(transport_config)
    if oidc is not None:
        return oidc

    if transport_config.http_bearer_token:
        return StaticTokenVerifier(
            tokens={
                transport_config.http_bearer_token: {
                    "client_id": "authenticated-client",
                    "scopes": ["read", "write"],
                }
            }
        )
    return None
