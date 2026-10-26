"""
Surfacing upstream GLPI failures to the MCP client.

Returning ``{"error": ...}`` from a tool marks the call *successful*: the result
carries no ``isError``, so a client cannot tell a failure from an empty result.
Raising ``ToolError`` sets the error flag, and FastMCP delivers its message to
the client unmasked.
"""

import json
from typing import NoReturn

from fastmcp import Context
from fastmcp.exceptions import ToolError

from glpi_mcp.glpi_client import GlpiAPIError

HINTS = {
    400: "Fix the parameters named in the error; glpi_describe_schema lists the "
    "properties of each schema.",
    401: "The GLPI access token is missing, invalid or expired.",
    403: "The GLPI user lacks the right for this item or action, or the OAuth "
    "client lacks the scope (e.g. 'graphql' for GraphQL).",
    404: "The item or endpoint does not exist (check the ID and the path; "
    "glpi_list_endpoints lists the paths).",
    409: "The request conflicts with the current state; re-read and retry.",
}


def describe(exc: Exception) -> str:
    """Render an exception, keeping GLPI's error body when there is one.

    Args:
        exc: The exception raised while talking to GLPI.

    Returns:
        str: Human-readable description. For an HTTP error it includes the
            status, GLPI's JSON body and, for common statuses, a hint.
    """
    if isinstance(exc, GlpiAPIError):
        body = exc.body
        if not isinstance(body, str):
            body = json.dumps(body, ensure_ascii=False, default=str)
        hint = HINTS.get(exc.status)
        if exc.token_rejected:
            hint = HINTS[401]
        if exc.path == "token":
            hint = (
                "GLPI refused the OAuth password grant: check GLPI_CLIENT_ID, "
                "GLPI_CLIENT_SECRET, GLPI_USERNAME, GLPI_PASSWORD, that the OAuth "
                "client allows the Password grant and the scopes in GLPI_SCOPES."
            )
        text = f"HTTP {exc.status} from GLPI ({exc.method} {exc.path}): {body}"
        return f"{text}. {hint}" if hint else text
    # Some exceptions carry no message at all (asyncio.TimeoutError), so the
    # type name has to stand on its own.
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


async def fail(ctx: Context, action: str, exc: Exception) -> NoReturn:
    """Log a failed tool call to the session and raise it as a tool error.

    Args:
        ctx: The MCP context of the running tool.
        action: What was being attempted, e.g. ``"Error searching tickets"``.
        exc: The exception that ended the call.

    Raises:
        ToolError: Always. This is how the failure reaches the client.
    """
    detail = f"{action}: {describe(exc)}"
    await ctx.error(detail)
    raise ToolError(detail) from exc
