#!/usr/bin/env python3
"""
GLPI MCP Server

Provides a Model Context Protocol (MCP) server exposing tools that interact with
the GLPI High-Level API (GLPI 11.0.6+ and 12).
"""

import logging
import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp import settings
from fastmcp.server.middleware.rate_limiting import SlidingWindowRateLimitingMiddleware
from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware
from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.server.transforms.search import RegexSearchTransform
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from glpi_mcp.auth import build_auth_provider
from glpi_mcp.auth import rate_limit_client_id
from glpi_mcp.glpi_client import get_glpi_config_from_env
from glpi_mcp.glpi_client import get_transport_config_from_env
from glpi_mcp.sentry_init import init_sentry
from glpi_mcp.tools import register_tools
from glpi_mcp.utils import HEALTH_PATH
from glpi_mcp.utils import HTTP_TRANSPORTS
from glpi_mcp.utils import TRANSPORT_ALIASES
from glpi_mcp.utils import VALID_TRANSPORTS

# Load environment variables
load_dotenv()

# Configure FastMCP defaults
settings.show_server_banner = False
settings.check_for_updates = "off"

# Configure logging. An unknown or lowercase LOG_LEVEL must not take the server
# down, so resolve it leniently and fall back to INFO.
_requested_log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
_resolved_log_level = logging.getLevelNamesMapping().get(_requested_log_level)
logging.basicConfig(
    level=_resolved_log_level if _resolved_log_level is not None else logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Initialize optional Sentry monitoring. This runs after logging is configured
# so that its "enabled" message is not dropped.
init_sentry()

if _resolved_log_level is None:
    logger.warning(
        "Unknown LOG_LEVEL %r - falling back to INFO. Valid values: %s",
        _requested_log_level,
        ", ".join(logging.getLevelNamesMapping()),
    )

# Get package version
try:
    __version__ = version("glpi-mcp")
except PackageNotFoundError:
    __version__ = "0.0.1"

try:
    GLPI_CONFIG = get_glpi_config_from_env()
    TRANSPORT_CONFIG = get_transport_config_from_env()
    auth_provider = build_auth_provider(GLPI_CONFIG, TRANSPORT_CONFIG)
except Exception as e:
    logger.error(f"Invalid configuration: {e}")
    raise


def build_instructions() -> str:
    """Describe the server to the model, including the active safety modes."""
    parts = [
        "This MCP server exposes tools for the GLPI IT service management API "
        "(High-Level API). Dedicated tools cover tickets, changes and problems "
        "(glpi_search_itil, glpi_get_itil_timeline, glpi_add_itil_followup...), "
        "assets, users, groups, entities, the knowledge base and documents. "
        "Generic tools (glpi_list_items, glpi_get_item, glpi_create_item...) work "
        "for every endpoint; glpi_list_endpoints lists the paths and "
        "glpi_describe_schema the properties to filter on or write. Resolve "
        "users, groups and categories to numeric IDs before filtering or writing."
    ]
    if GLPI_CONFIG.read_only_mode:
        parts.append("The server is in READ-ONLY mode: no write tools are available.")
    elif GLPI_CONFIG.dry_run_mode:
        parts.append(
            "The server is in DRY-RUN mode: write tools only return the request "
            "they would send and never change GLPI. Tell the user that changes "
            "were simulated."
        )
    return " ".join(parts)


# Initialize FastMCP server
mcp = FastMCP(
    name="GLPI MCP Server",
    version=__version__,
    instructions=build_instructions(),
    auth=auth_provider,
)


@mcp.custom_route(HEALTH_PATH, methods=["GET"], include_in_schema=False)
async def health(_request: Request) -> PlainTextResponse:
    """Liveness probe for the HTTP transports.

    Answering proves the server handles requests, which a TCP connect does not:
    the kernel completes the handshake even while the event loop is stuck. It
    deliberately does not call GLPI, so an outage there does not get a
    healthy MCP server restarted. FastMCP serves custom routes outside the
    bearer-token and OAuth auth, so the container healthcheck needs no token.
    """
    return PlainTextResponse("ok")


# Register all tools
register_tools(mcp, GLPI_CONFIG)


def configure_component_visibility() -> None:
    """Apply server-level visibility transforms for read-only and disabled tags."""

    if GLPI_CONFIG.read_only_mode:
        logger.info("Read-only mode is enabled - restricting to read-only components")
        mcp.enable(tags={"read-only"}, only=True)

    if GLPI_CONFIG.disabled_tags:
        logger.info(
            "Disabled tags configured: %s - disabling matching components",
            GLPI_CONFIG.disabled_tags,
        )
        mcp.disable(tags=GLPI_CONFIG.disabled_tags)


def configure_tool_search() -> None:
    """Apply the optional FastMCP tool-search transform."""

    if not GLPI_CONFIG.tool_search_enabled:
        return

    strategy = GLPI_CONFIG.tool_search_strategy
    max_results = GLPI_CONFIG.tool_search_max_results

    if strategy == "regex":
        mcp.add_transform(RegexSearchTransform(max_results=max_results))
    else:
        mcp.add_transform(BM25SearchTransform(max_results=max_results))

    logger.info(
        "Tool search is enabled - strategy=%s, max_results=%s",
        strategy,
        max_results,
    )


configure_component_visibility()
configure_tool_search()

# Optional response size limit
if GLPI_CONFIG.response_max_size:
    logger.info(
        "Response size limit is enabled - max %d bytes", GLPI_CONFIG.response_max_size
    )
    mcp.add_middleware(
        ResponseLimitingMiddleware(max_size=GLPI_CONFIG.response_max_size)
    )

# Optional rate limiting
if GLPI_CONFIG.rate_limit_enabled:
    logger.info("Rate limiting is enabled - applying middleware")
    mcp.add_middleware(
        SlidingWindowRateLimitingMiddleware(
            max_requests=GLPI_CONFIG.rate_limit_max_requests,
            window_minutes=GLPI_CONFIG.rate_limit_window_minutes,
            get_client_id=rate_limit_client_id,
        )
    )


def auth_label() -> str | None:
    """Describe how MCP clients authenticate, for the startup log."""
    if GLPI_CONFIG.oauth_login:
        return "GLPI OAuth login (each user signs in with GLPI)"
    if GLPI_CONFIG.token_passthrough:
        return "per-user GLPI token passthrough"
    if TRANSPORT_CONFIG.oidc_enabled:
        return "OIDC"
    if TRANSPORT_CONFIG.http_bearer_token:
        return "bearer token"
    return None


def main():
    # A typo must not fall back to stdio: the HTTP port would never open while
    # the container healthcheck, which skips non-HTTP transports, kept reporting
    # healthy. Checked here rather than at import, because `fastmcp run` imports
    # this module but picks the transport itself.
    if TRANSPORT_CONFIG.transport_type not in VALID_TRANSPORTS:
        logger.error(
            "Unknown MCP_TRANSPORT %r. Valid values: %s",
            TRANSPORT_CONFIG.transport_type,
            ", ".join([*sorted(VALID_TRANSPORTS), *TRANSPORT_ALIASES]),
        )
        raise SystemExit(1)

    if not GLPI_CONFIG.glpi_url:
        logger.error("Missing required GLPI URL (GLPI_URL). Check your .env file.")
        raise SystemExit(1)

    # Without a scheme the request URL is ambiguous, and a silent fallback to
    # http:// would put passwords and tokens on the wire in cleartext.
    scheme = urlparse(GLPI_CONFIG.glpi_url).scheme.lower()
    if scheme not in {"http", "https"}:
        logger.error(
            "GLPI_URL must start with https:// or http:// (got %r).",
            GLPI_CONFIG.glpi_url,
        )
        raise SystemExit(1)
    if scheme == "http":
        logger.warning(
            "GLPI_URL uses plaintext http://. The OAuth password grant and every "
            "access token cross the network in the clear - use https:// outside "
            "a trusted network."
        )

    missing = GLPI_CONFIG.missing_credentials()
    if missing:
        logger.error(
            "Missing GLPI OAuth settings: %s. Check your .env file.", ", ".join(missing)
        )
        raise SystemExit(1)
    if GLPI_CONFIG.per_user_tokens and (GLPI_CONFIG.username or GLPI_CONFIG.password):
        logger.warning(
            "GLPI_USERNAME/GLPI_PASSWORD are ignored: every tool call runs with "
            "the calling user's own GLPI token."
        )

    label = auth_label()
    if TRANSPORT_CONFIG.transport_type in HTTP_TRANSPORTS and label is None:
        logger.warning(
            "WARNING: MCP_HTTP_BEARER_TOKEN is not set. The MCP server will run WITHOUT authentication. "
            "Ensure the server is not exposed to untrusted networks (e.g. bind to 127.0.0.1 instead of 0.0.0.0)."
        )

    modes = [
        name
        for name, enabled in (
            ("read-only", GLPI_CONFIG.read_only_mode),
            ("dry-run", GLPI_CONFIG.dry_run_mode),
        )
        if enabled
    ]
    logger.info(
        "Starting GLPI MCP Server connecting to %s (API version: %s, modes: %s)...",
        GLPI_CONFIG.glpi_url,
        GLPI_CONFIG.api_version,
        ", ".join(modes) or "read-write",
    )

    # Choose transport based on configuration
    if TRANSPORT_CONFIG.transport_type in HTTP_TRANSPORTS:
        # Spelled out as literals: mcp.run() takes a Literal transport name.
        transport = "sse" if TRANSPORT_CONFIG.transport_type == "sse" else "http"
        kind = "SSE" if transport == "sse" else "Streamable"
        logger.info(
            f"Using HTTP {kind} transport on {TRANSPORT_CONFIG.http_host}:{TRANSPORT_CONFIG.http_port}"
        )
        if label:
            logger.info(f"Authentication for {kind} transport: {label}")

        mcp.run(
            transport=transport,
            host=TRANSPORT_CONFIG.http_host,
            port=TRANSPORT_CONFIG.http_port,
        )
    else:
        # STDIO transport
        logger.info("Using STDIO transport")
        mcp.run()


if __name__ == "__main__":
    main()
