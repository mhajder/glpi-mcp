"""
Asset tools: computers, monitors, network equipment, custom assets...
"""

from typing import Annotated
from typing import Any

import httpx2
from fastmcp import Context
from pydantic import Field

from glpi_mcp.glpi_client import GlpiAPIError
from glpi_mcp.glpi_client import GlpiClient
from glpi_mcp.models import GlpiConfig
from glpi_mcp.rsql import combine
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
from glpi_mcp.tools.common import any_of
from glpi_mcp.tools.common import api_path
from glpi_mcp.tools.common import delete
from glpi_mcp.tools.common import plain_text
from glpi_mcp.tools.common import query_filter
from glpi_mcp.tools.common import search
from glpi_mcp.tools.common import write
from glpi_mcp.tools.errors import fail

AssetTypeArg = Annotated[
    str,
    Field(
        description="Asset type, e.g. 'Computer', 'Monitor', 'NetworkEquipment', "
        "'Peripheral', 'Phone', 'Printer', 'Software', 'Rack', or a custom asset "
        "as 'Custom/<name>'. glpi_list_asset_types lists them.",
    ),
]
OptionalAssetTypeArg = Annotated[
    str | None,
    Field(
        default=None,
        description="Asset type (see glpi_list_asset_types). Omit to search all "
        "asset types at once.",
    ),
]


def register_asset_tools(
    mcp, config: GlpiConfig, transport: httpx2.AsyncBaseTransport | None = None
):
    """Register asset tools with the MCP server"""

    @mcp.tool(
        tags={"glpi", "assets", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_list_asset_types(ctx: Context) -> dict:
        """List the asset types this GLPI has, including custom asset definitions."""
        try:
            async with GlpiClient(config, transport=transport) as glpi:
                doc = await glpi.openapi()
            types = set()
            for raw_path, operations in (doc.get("paths") or {}).items():
                parts = raw_path.strip("/").split("/")
                if (
                    len(parts) in (2, 3)
                    and parts[0] == "Assets"
                    and parts[1] != "Global"
                    # "Assets/Custom" alone is not a type, only its children are.
                    and parts[1:] != ["Custom"]
                    and isinstance(operations, dict)
                    and "get" in operations
                    and "{" not in raw_path
                ):
                    # Custom assets live one level deeper: Assets/Custom/<name>.
                    if parts[1] == "Custom" and len(parts) == 3:
                        types.add(f"Custom/{parts[2]}")
                    elif len(parts) == 2:
                        types.add(parts[1])
            return {"count": len(types), "asset_types": sorted(types)}
        except Exception as e:
            await fail(ctx, "Error listing asset types", e)

    @mcp.tool(
        tags={"glpi", "assets", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_search_assets(
        ctx: Context,
        asset_type: OptionalAssetTypeArg = None,
        name: Annotated[
            str | None,
            Field(
                default=None, description="Part of the asset name (case-insensitive)."
            ),
        ] = None,
        serial: Annotated[
            str | None,
            Field(default=None, description="Serial number (exact)."),
        ] = None,
        otherserial: Annotated[
            str | None,
            Field(default=None, description="Inventory number (exact)."),
        ] = None,
        entity_id: Annotated[int | None, Field(default=None, ge=0)] = None,
        filters: FiltersArg = None,
        filter: FilterArg = None,
        sort: SortArg = None,
        fields: FieldsArg = None,
        start: StartArg = 0,
        limit: LimitArg = 20,
    ) -> dict:
        """Search assets of one type, or of all types at once.

        Searching all types ('Assets/Global') returns each asset with its type;
        filter one type for type-specific properties (glpi_describe_schema
        'Computer').
        """
        try:
            endpoint = api_path("Assets", asset_type or "Global")
            clauses: dict[str, Any] = {}
            if serial:
                clauses["serial"] = serial
            if otherserial:
                clauses["otherserial"] = otherserial
            if entity_id is not None:
                clauses["entity.id"] = entity_id
            rsql = combine(
                [
                    any_of(("name",), name),
                    query_filter(clauses, None),
                    query_filter(filters, filter),
                ]
            )
            return await search(
                config,
                transport,
                endpoint,
                rsql,
                sort=sort,
                start=start,
                limit=limit,
                fields=fields,
            )
        except Exception as e:
            await fail(ctx, "Error searching assets", e)

    @mcp.tool(
        tags={"glpi", "assets", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_get_asset(
        ctx: Context,
        asset_type: AssetTypeArg,
        asset_id: ItemIdArg,
        include_infocom: Annotated[
            bool,
            Field(
                default=False,
                description="Also return financial and administrative information "
                "(purchase, warranty, supplier).",
            ),
        ] = False,
    ) -> Any:
        """Get one asset by type and ID."""
        try:
            endpoint = api_path("Assets", asset_type, asset_id)
            async with GlpiClient(config, transport=transport) as glpi:
                asset = plain_text(await glpi.get(endpoint))
                if include_infocom and isinstance(asset, dict):
                    try:
                        asset["infocom"] = plain_text(
                            await glpi.get(f"{endpoint}/Infocom")
                        )
                    except GlpiAPIError as exc:
                        # GLPI answers 404 for an asset with no financial
                        # information recorded yet.
                        if exc.status != 404:
                            raise
                        asset["infocom"] = None
            return asset
        except Exception as e:
            await fail(ctx, f"Error getting {asset_type} {asset_id}", e)

    @mcp.tool(
        tags={"glpi", "assets", "write"},
        annotations=CREATE_ANNOTATIONS,
    )
    async def glpi_create_asset(
        ctx: Context,
        asset_type: AssetTypeArg,
        payload: Annotated[
            dict[str, Any],
            Field(
                description="Asset properties, e.g. {'name': 'PC-042', 'serial': "
                "'ABC123', 'entity': 0, 'location': 3}. See glpi_describe_schema."
            ),
        ],
    ) -> dict:
        """Create an asset."""
        try:
            endpoint = api_path("Assets", asset_type)
            return await write(config, transport, "POST", endpoint, payload, "create")
        except Exception as e:
            await fail(ctx, f"Error creating {asset_type}", e)

    @mcp.tool(
        tags={"glpi", "assets", "write"},
        annotations=UPDATE_ANNOTATIONS,
    )
    async def glpi_update_asset(
        ctx: Context,
        asset_type: AssetTypeArg,
        asset_id: ItemIdArg,
        changes: Annotated[
            dict[str, Any],
            Field(description="Properties to change, e.g. {'location': 4}."),
        ],
    ) -> Any:
        """Update an asset; only the given properties change."""
        try:
            endpoint = api_path("Assets", asset_type, asset_id)
            return await write(config, transport, "PATCH", endpoint, changes, "update")
        except Exception as e:
            await fail(ctx, f"Error updating {asset_type} {asset_id}", e)

    @mcp.tool(
        tags={"glpi", "assets", "write"},
        annotations=DELETE_ANNOTATIONS,
    )
    async def glpi_delete_asset(
        ctx: Context,
        asset_type: AssetTypeArg,
        asset_id: ItemIdArg,
        force: ForceArg = False,
    ) -> dict:
        """Delete an asset: to the trash, or purged with force=true."""
        try:
            return await delete(
                config, transport, api_path("Assets", asset_type, asset_id), force
            )
        except Exception as e:
            await fail(ctx, f"Error deleting {asset_type} {asset_id}", e)
