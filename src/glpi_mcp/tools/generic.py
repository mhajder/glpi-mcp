"""
Generic tools that work for every High-Level API endpoint.
"""

from typing import Annotated
from typing import Any

import httpx2
from fastmcp import Context
from pydantic import Field

from glpi_mcp.glpi_client import GlpiClient
from glpi_mcp.models import GlpiConfig
from glpi_mcp.tools.common import CREATE_ANNOTATIONS
from glpi_mcp.tools.common import DELETE_ANNOTATIONS
from glpi_mcp.tools.common import READ_ANNOTATIONS
from glpi_mcp.tools.common import READ_ONLY_TAG
from glpi_mcp.tools.common import UPDATE_ANNOTATIONS
from glpi_mcp.tools.common import FieldsArg
from glpi_mcp.tools.common import FilterArg
from glpi_mcp.tools.common import FiltersArg
from glpi_mcp.tools.common import ForceArg
from glpi_mcp.tools.common import ItemIdArg
from glpi_mcp.tools.common import LimitArg
from glpi_mcp.tools.common import SortArg
from glpi_mcp.tools.common import StartArg
from glpi_mcp.tools.common import api_path
from glpi_mcp.tools.common import delete
from glpi_mcp.tools.common import plain_text
from glpi_mcp.tools.common import query_filter
from glpi_mcp.tools.common import search
from glpi_mcp.tools.common import write
from glpi_mcp.tools.errors import fail

HTTP_METHODS = ("get", "post", "patch", "put", "delete")
# Longest endpoint description kept in glpi_list_endpoints.
MAX_SUMMARY = 160
# Schema keywords kept by glpi_describe_schema; the rest (x-* internals,
# SQL join definitions) only cost tokens.
SCHEMA_KEYS = ("type", "format", "enum", "description", "readOnly", "items")

PathArg = Annotated[
    str,
    Field(
        description="Endpoint path relative to the API version root, e.g. "
        "'Assistance/Ticket', 'Assets/Computer', 'Management/Contract', "
        "'Dropdowns/Location'. glpi_list_endpoints lists every path.",
    ),
]
RawHtmlArg = Annotated[
    bool,
    Field(
        default=False,
        description="Keep rich-text properties (content, comment...) as HTML "
        "instead of converting them to plain text.",
    ),
]
PayloadArg = Annotated[
    dict[str, Any],
    Field(
        description="Item properties as named in the schema (glpi_describe_schema). "
        "Dropdown/link properties take an ID: {'entity': 0} or {'entity': {'id': 0}}.",
    ),
]


def _summarize_schema(schema: Any, depth: int = 0) -> Any:
    """Keep the parts of a JSON schema a model needs to read or write an item."""
    if not isinstance(schema, dict):
        return schema
    if "$ref" in schema:
        return {"$ref": schema["$ref"].rsplit("/", 1)[-1]}
    result = {key: schema[key] for key in SCHEMA_KEYS if key in schema}
    if isinstance(result.get("description"), str):
        result["description"] = result["description"].strip()
    if "items" in result:
        result["items"] = _summarize_schema(result["items"], depth + 1)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        if depth >= 2:
            result["properties"] = sorted(properties)
        else:
            result["properties"] = {
                name: _summarize_schema(prop, depth + 1)
                for name, prop in properties.items()
            }
    return result


def register_generic_tools(
    mcp, config: GlpiConfig, transport: httpx2.AsyncBaseTransport | None = None
):
    """Register generic endpoint tools with the MCP server"""

    @mcp.tool(
        tags={"glpi", "generic", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_list_endpoints(
        ctx: Context,
        prefix: Annotated[
            str | None,
            Field(
                default=None,
                description="Only paths starting with this, e.g. 'Assets', "
                "'Management', 'Dropdowns', 'Assistance/Ticket'.",
            ),
        ] = None,
    ) -> dict:
        """List the High-Level API endpoints with their HTTP methods.

        Read from GLPI's own OpenAPI document, so it matches the GLPI version
        and API version in use, including custom asset types. Use the paths
        with the generic tools (glpi_list_items, glpi_get_item, ...).
        """
        try:
            async with GlpiClient(config, transport=transport) as glpi:
                doc = await glpi.openapi()
            wanted = (prefix or "").strip("/").lower()
            endpoints = []
            for raw_path, operations in sorted((doc.get("paths") or {}).items()):
                path = raw_path.strip("/")
                if wanted and not path.lower().startswith(wanted):
                    continue
                if not isinstance(operations, dict):
                    continue
                methods = [m.upper() for m in HTTP_METHODS if m in operations]
                if not methods:
                    continue
                first = operations[methods[0].lower()]
                summary = ""
                if isinstance(first, dict):
                    summary = str(
                        first.get("summary") or first.get("description") or ""
                    ).strip()
                entry: dict[str, Any] = {"path": path, "methods": methods}
                if summary:
                    entry["summary"] = summary[:MAX_SUMMARY]
                endpoints.append(entry)
            return {"count": len(endpoints), "endpoints": endpoints}
        except Exception as e:
            await fail(ctx, "Error listing endpoints", e)

    @mcp.tool(
        tags={"glpi", "generic", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_describe_schema(
        ctx: Context,
        name: Annotated[
            str | None,
            Field(
                default=None,
                description="Schema name, e.g. 'Ticket', 'Computer', 'User', "
                "'Followup', 'TicketTask'. Omit to list all schema names.",
            ),
        ] = None,
    ) -> dict:
        """Describe a schema: its properties, types, enums and read-only flags.

        Property names are what filters, sorting and writes use; nested objects
        are filtered with dot paths (e.g. 'status.id', 'entity.id').
        """
        try:
            async with GlpiClient(config, transport=transport) as glpi:
                doc = await glpi.openapi()
            schemas = (doc.get("components") or {}).get("schemas") or {}
            if not name:
                return {"count": len(schemas), "schemas": sorted(schemas)}
            schema = schemas.get(name)
            if schema is None:
                matches = [s for s in schemas if s.lower() == name.lower()]
                if not matches:
                    close = sorted(s for s in schemas if name.lower() in s.lower())
                    raise ValueError(
                        f"No schema named {name!r}."
                        + (f" Similar: {', '.join(close[:20])}" if close else "")
                    )
                name = matches[0]
                schema = schemas[name]
            return {"name": name, "schema": _summarize_schema(schema)}
        except Exception as e:
            await fail(ctx, "Error describing schema", e)

    @mcp.tool(
        tags={"glpi", "generic", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_list_items(
        ctx: Context,
        path: PathArg,
        filters: FiltersArg = None,
        filter: FilterArg = None,
        sort: SortArg = None,
        fields: FieldsArg = None,
        start: StartArg = 0,
        limit: LimitArg = 20,
        raw_html: RawHtmlArg = False,
    ) -> dict:
        """List the items of any collection endpoint, with RSQL filtering.

        Works for every collection, e.g. 'Assets/Computer', 'Management/Contract',
        'Dropdowns/Location', 'Administration/User'. Returns one page plus the
        total; continue with 'start' = 'next_start' while 'has_more' is true.
        """
        try:
            return await search(
                config,
                transport,
                api_path(path),
                query_filter(filters, filter),
                sort=sort,
                start=start,
                limit=limit,
                fields=fields,
                raw_html=raw_html,
            )
        except Exception as e:
            await fail(ctx, "Error listing items", e)

    @mcp.tool(
        tags={"glpi", "generic", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_get_item(
        ctx: Context,
        path: PathArg,
        item_id: Annotated[
            int | None,
            Field(
                default=None,
                ge=1,
                description="Item ID, appended to the path. Omit when the path "
                "already names one item or a non-collection resource.",
            ),
        ] = None,
        raw_html: RawHtmlArg = False,
    ) -> Any:
        """Get one item (or any GET resource) by endpoint path and ID.

        For example path='Assets/Computer', item_id=12, or
        path='Assistance/Ticket/5/TeamMember'.
        """
        try:
            endpoint = api_path(path, *([item_id] if item_id else []))
            async with GlpiClient(config, transport=transport) as glpi:
                item = await glpi.get(endpoint)
            return item if raw_html else plain_text(item)
        except Exception as e:
            await fail(ctx, "Error getting item", e)

    @mcp.tool(
        tags={"glpi", "generic", "write"},
        annotations=CREATE_ANNOTATIONS,
    )
    async def glpi_create_item(
        ctx: Context,
        path: PathArg,
        payload: PayloadArg,
    ) -> dict:
        """Create an item at any collection endpoint (POST).

        Check the writable properties with glpi_describe_schema first. Returns
        the new item's ID and link.
        """
        try:
            return await write(
                config, transport, "POST", api_path(path), payload, "create"
            )
        except Exception as e:
            await fail(ctx, "Error creating item", e)

    @mcp.tool(
        tags={"glpi", "generic", "write"},
        annotations=UPDATE_ANNOTATIONS,
    )
    async def glpi_update_item(
        ctx: Context,
        path: PathArg,
        item_id: ItemIdArg,
        payload: PayloadArg,
    ) -> Any:
        """Update an item at any endpoint (PATCH); only the given properties change."""
        try:
            return await write(
                config, transport, "PATCH", api_path(path, item_id), payload, "update"
            )
        except Exception as e:
            await fail(ctx, "Error updating item", e)

    @mcp.tool(
        tags={"glpi", "generic", "write"},
        annotations=DELETE_ANNOTATIONS,
    )
    async def glpi_delete_item(
        ctx: Context,
        path: PathArg,
        item_id: ItemIdArg,
        force: ForceArg = False,
    ) -> dict:
        """Delete an item at any endpoint: to the trash, or purged with force=true."""
        try:
            return await delete(config, transport, api_path(path, item_id), force)
        except Exception as e:
            await fail(ctx, "Error deleting item", e)

    @mcp.tool(
        tags={"glpi", "generic", "graphql", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_graphql_query(
        ctx: Context,
        query: Annotated[
            str,
            Field(
                description="GraphQL query, e.g. '{ Ticket(limit: 5) { id name "
                "status { id name } } }'. Types are the schema names; arguments "
                "include filter (RSQL), start, limit and sort."
            ),
        ],
    ) -> Any:
        """Run a read-only GraphQL query, to fetch nested data in one call.

        Needs the 'graphql' OAuth scope (add it to GLPI_SCOPES and allow it on
        the OAuth client).
        """
        try:
            async with GlpiClient(config, transport=transport) as glpi:
                return await glpi.post("GraphQL", json={"query": query}, read_only=True)
        except Exception as e:
            await fail(ctx, "Error running GraphQL query", e)
