"""
Server, session and identity tools.
"""

from typing import Any

import httpx2
from fastmcp import Context

from glpi_mcp.glpi_client import GlpiAPIError
from glpi_mcp.glpi_client import GlpiClient
from glpi_mcp.models import GlpiConfig
from glpi_mcp.tools.common import READ_ANNOTATIONS
from glpi_mcp.tools.common import READ_ONLY_TAG
from glpi_mcp.tools.errors import fail


def auth_mode(config: GlpiConfig) -> str:
    """Name the way this server authenticates to GLPI."""
    if config.oauth_login:
        return "glpi-login (each user's own GLPI account)"
    if config.token_passthrough:
        return "token-passthrough (each caller's own GLPI token)"
    return f"password-grant (as {config.username})"


# Keys of the active profile worth showing; the rest (a rights table and the
# allowed status transitions) is several KB that no tool needs.
PROFILE_SUMMARY_KEYS = ("id", "name", "interface")


def compact_session(session: Any) -> Any:
    """Drop the active profile's rights table from a /session answer."""
    if not isinstance(session, dict):
        return session
    profile = session.get("active_profile")
    if isinstance(profile, dict):
        session = {
            **session,
            "active_profile": {
                k: profile[k] for k in PROFILE_SUMMARY_KEYS if k in profile
            },
        }
    return session


def register_session_tools(
    mcp, config: GlpiConfig, transport: httpx2.AsyncBaseTransport | None = None
):
    """Register server and session tools with the MCP server"""

    @mcp.tool(
        tags={"glpi", "session", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_server_info(ctx: Context) -> dict:
        """Show which GLPI this server talks to and how.

        Returns the High-Level API versions GLPI offers, the version every call
        is pinned to, the authentication mode and whether the server is
        read-only or in dry-run mode. Call this first when a request fails in a
        way that suggests a version or permission problem.
        """
        try:
            async with GlpiClient(config, transport=transport) as glpi:
                versions = await glpi.api_versions()
                selected = await glpi.api_version()
            return {
                "glpi_url": config.glpi_url,
                "api_versions": [
                    {
                        "version": v.get("version"),
                        "deprecated": bool(v.get("deprecated", False)),
                    }
                    for v in versions
                ],
                "api_version": selected,
                "api_version_setting": config.api_version,
                "auth_mode": auth_mode(config),
                "entity_id": config.entity_id,
                "entity_recursive": config.entity_recursive,
                "profile_id": config.profile_id,
                "read_only_mode": config.read_only_mode,
                "dry_run_mode": config.dry_run_mode,
            }
        except Exception as e:
            await fail(ctx, "Error reading GLPI server info", e)

    @mcp.tool(
        tags={"glpi", "session", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_whoami(ctx: Context) -> dict:
        """Show the GLPI user the tools act as, with the active profile and entities.

        Combines the API session (user ID, active profile, active entity) with
        the user's own record when the OAuth token has the 'user' scope.
        """
        try:
            async with GlpiClient(config, transport=transport) as glpi:
                session = await glpi.get("session")
                user = None
                try:
                    user = await glpi.get("Administration/User/Me")
                except GlpiAPIError as exc:
                    # /User/Me needs the 'user' scope; the session alone still
                    # answers the question.
                    if exc.status not in (401, 403):
                        raise
            return {"session": compact_session(session), "user": user}
        except Exception as e:
            await fail(ctx, "Error reading the GLPI session", e)

    @mcp.tool(
        tags={"glpi", "session", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_get_entity_tree(ctx: Context) -> dict:
        """Return the tree of entities the current user can work in.

        Use the entity IDs to filter searches (e.g. {'entity.id': 3}) or to set
        an item's entity on create.
        """
        try:
            async with GlpiClient(config, transport=transport) as glpi:
                return {"entity_tree": await glpi.get("Session/EntityTree")}
        except Exception as e:
            await fail(ctx, "Error reading the entity tree", e)
