"""
Users, groups, entities and profiles.
"""

import re
from typing import Annotated
from typing import Any

import httpx2
from fastmcp import Context
from pydantic import Field

from glpi_mcp.glpi_client import GlpiClient
from glpi_mcp.models import GlpiConfig
from glpi_mcp.rsql import build_filter
from glpi_mcp.rsql import combine
from glpi_mcp.tools import common
from glpi_mcp.tools.common import READ_ANNOTATIONS
from glpi_mcp.tools.common import READ_ONLY_TAG
from glpi_mcp.tools.common import FieldsArg
from glpi_mcp.tools.common import FilterArg
from glpi_mcp.tools.common import FiltersArg
from glpi_mcp.tools.common import ItemIdArg
from glpi_mcp.tools.common import LimitArg
from glpi_mcp.tools.common import SortArg
from glpi_mcp.tools.common import StartArg
from glpi_mcp.tools.common import any_of
from glpi_mcp.tools.common import plain_text
from glpi_mcp.tools.common import query_filter
from glpi_mcp.tools.errors import fail

# Login names GLPI's /User/username/{username} route accepts.
USERNAME_ROUTE = re.compile(r"^[A-Za-z0-9_]+$")
# Default properties of a user listing; full users carry emails, preferences...
USER_SUMMARY_FIELDS = [
    "id",
    "username",
    "realname",
    "firstname",
    "is_active",
    "default_entity",
    "emails",
    "phone",
]

TextArg = Annotated[
    str | None,
    Field(default=None, description="Part of the name (case-insensitive)."),
]


def register_administration_tools(
    mcp, config: GlpiConfig, transport: httpx2.AsyncBaseTransport | None = None
):
    """Register user, group, entity and profile tools with the MCP server"""

    async def search(
        endpoint: str,
        text_filter: str | None,
        filters: dict[str, Any] | None,
        filter: str | None,
        sort: str | None,
        fields: list[str] | None,
        start: int,
        limit: int,
    ) -> dict:
        return await common.search(
            config,
            transport,
            endpoint,
            combine([text_filter, query_filter(filters, filter)]),
            sort=sort,
            start=start,
            limit=limit,
            fields=fields,
        )

    @mcp.tool(
        tags={"glpi", "administration", "users", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_search_users(
        ctx: Context,
        text: Annotated[
            str | None,
            Field(
                default=None,
                description="Part of the login, first name or last name "
                "(case-insensitive).",
            ),
        ] = None,
        active_only: Annotated[
            bool, Field(default=True, description="Leave out disabled users.")
        ] = True,
        filters: FiltersArg = None,
        filter: FilterArg = None,
        sort: SortArg = None,
        fields: FieldsArg = None,
        start: StartArg = 0,
        limit: LimitArg = 20,
    ) -> dict:
        """Search GLPI users, e.g. to find the ID of a requester or technician."""
        try:
            text_filter = combine(
                [
                    any_of(("username", "firstname", "realname"), text),
                    "is_active==1" if active_only else None,
                ]
            )
            return await search(
                "Administration/User",
                text_filter,
                filters,
                filter,
                sort,
                fields or USER_SUMMARY_FIELDS,
                start,
                limit,
            )
        except Exception as e:
            await fail(ctx, "Error searching users", e)

    @mcp.tool(
        tags={"glpi", "administration", "users", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_get_user(
        ctx: Context,
        user_id: Annotated[int | None, Field(default=None, ge=1)] = None,
        username: Annotated[
            str | None, Field(default=None, description="Login name.")
        ] = None,
    ) -> Any:
        """Get one user by ID or login name."""
        try:
            if (user_id is None) == (username is None):
                raise ValueError("Give exactly one of 'user_id' and 'username'.")
            async with GlpiClient(config, transport=transport) as glpi:
                if user_id is not None:
                    return plain_text(await glpi.get(f"Administration/User/{user_id}"))
                if username and USERNAME_ROUTE.match(username):
                    return plain_text(
                        await glpi.get(f"Administration/User/username/{username}")
                    )
                # GLPI's username route only takes letters, digits and "_", so
                # a login such as "j.doe" or an email is looked up by filter.
                result = await glpi.collection(
                    "Administration/User",
                    filter=build_filter({"username": username}),
                    limit=10,
                )
            if not result.rows:
                raise ValueError(f"No user with login {username!r}.")
            if len(result.rows) > 1:
                # One login may exist once per authentication source (local,
                # LDAP...); picking one silently could assign the wrong user.
                ids = [row.get("id") for row in result.rows if isinstance(row, dict)]
                raise ValueError(
                    f"Several users have the login {username!r} (IDs {ids}); "
                    "use glpi_get_user with user_id."
                )
            return plain_text(result.rows[0])
        except Exception as e:
            await fail(ctx, "Error getting user", e)

    @mcp.tool(
        tags={"glpi", "administration", "groups", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_search_groups(
        ctx: Context,
        text: TextArg = None,
        filters: FiltersArg = None,
        filter: FilterArg = None,
        sort: SortArg = None,
        fields: FieldsArg = None,
        start: StartArg = 0,
        limit: LimitArg = 20,
    ) -> dict:
        """Search groups, e.g. to find the ID of a technician group."""
        try:
            return await search(
                "Administration/Group",
                any_of(("name", "completename"), text),
                filters,
                filter,
                sort,
                fields,
                start,
                limit,
            )
        except Exception as e:
            await fail(ctx, "Error searching groups", e)

    @mcp.tool(
        tags={"glpi", "administration", "groups", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_get_group(ctx: Context, group_id: ItemIdArg) -> Any:
        """Get one group by ID."""
        try:
            async with GlpiClient(config, transport=transport) as glpi:
                return plain_text(await glpi.get(f"Administration/Group/{group_id}"))
        except Exception as e:
            await fail(ctx, f"Error getting group {group_id}", e)

    @mcp.tool(
        tags={"glpi", "administration", "entities", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_search_entities(
        ctx: Context,
        text: TextArg = None,
        filters: FiltersArg = None,
        filter: FilterArg = None,
        sort: SortArg = None,
        fields: FieldsArg = None,
        start: StartArg = 0,
        limit: LimitArg = 50,
    ) -> dict:
        """Search entities (organizational units that scope items and rights)."""
        try:
            return await search(
                "Administration/Entity",
                any_of(("name", "completename"), text),
                filters,
                filter,
                sort,
                fields,
                start,
                limit,
            )
        except Exception as e:
            await fail(ctx, "Error searching entities", e)

    @mcp.tool(
        tags={"glpi", "administration", "entities", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_get_entity(
        ctx: Context,
        entity_id: Annotated[
            int, Field(ge=0, description="Entity ID (0 is the root).")
        ],
    ) -> Any:
        """Get one entity by ID."""
        try:
            async with GlpiClient(config, transport=transport) as glpi:
                return plain_text(await glpi.get(f"Administration/Entity/{entity_id}"))
        except Exception as e:
            await fail(ctx, f"Error getting entity {entity_id}", e)

    @mcp.tool(
        tags={"glpi", "administration", "profiles", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_list_profiles(
        ctx: Context,
        start: StartArg = 0,
        limit: LimitArg = 50,
    ) -> dict:
        """List profiles (sets of rights such as Self-Service, Technician, Admin)."""
        try:
            return await search(
                "Administration/Profile", None, None, None, None, None, start, limit
            )
        except Exception as e:
            await fail(ctx, "Error listing profiles", e)
