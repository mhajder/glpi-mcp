"""
Document tools.

The High-Level API has no file upload, so documents can be found, read and
downloaded here but are uploaded through GLPI itself.
"""

import base64
from typing import Annotated
from typing import Any

import httpx2
from fastmcp import Context
from pydantic import Field

from glpi_mcp.glpi_client import GlpiClient
from glpi_mcp.models import GlpiConfig
from glpi_mcp.rsql import combine
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
from glpi_mcp.tools.common import search
from glpi_mcp.tools.errors import fail

DOCUMENTS = "Management/Document"
# Largest file returned inline; anything bigger would flood the model's context.
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/x-yaml")


def register_document_tools(
    mcp, config: GlpiConfig, transport: httpx2.AsyncBaseTransport | None = None
):
    """Register document tools with the MCP server"""

    @mcp.tool(
        tags={"glpi", "documents", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_search_documents(
        ctx: Context,
        text: Annotated[
            str | None,
            Field(
                default=None,
                description="Part of the document name or file name.",
            ),
        ] = None,
        filters: FiltersArg = None,
        filter: FilterArg = None,
        sort: SortArg = None,
        fields: FieldsArg = None,
        start: StartArg = 0,
        limit: LimitArg = 20,
    ) -> dict:
        """Search documents (files stored in GLPI)."""
        try:
            rsql = combine(
                [any_of(("name", "filename"), text), query_filter(filters, filter)]
            )
            return await search(
                config,
                transport,
                DOCUMENTS,
                rsql,
                sort=sort,
                start=start,
                limit=limit,
                fields=fields,
            )
        except Exception as e:
            await fail(ctx, "Error searching documents", e)

    @mcp.tool(
        tags={"glpi", "documents", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_get_document(ctx: Context, document_id: ItemIdArg) -> Any:
        """Get a document's metadata: name, file name, MIME type, checksum."""
        try:
            async with GlpiClient(config, transport=transport) as glpi:
                return plain_text(await glpi.get(f"{DOCUMENTS}/{document_id}"))
        except Exception as e:
            await fail(ctx, f"Error getting document {document_id}", e)

    @mcp.tool(
        tags={"glpi", "documents", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_download_document(
        ctx: Context,
        document_id: ItemIdArg,
        max_bytes: Annotated[
            int,
            Field(
                default=256 * 1024,
                ge=1,
                le=MAX_DOWNLOAD_BYTES,
                description="Refuse files larger than this many bytes.",
            ),
        ] = 256 * 1024,
    ) -> dict:
        """Download a document's file.

        Text files are returned as text, anything else as base64.
        """
        try:
            async with GlpiClient(config, transport=transport) as glpi:
                # Bounded while reading, so a huge file is never buffered.
                download = await glpi.download(
                    f"{DOCUMENTS}/{document_id}/Download", max_bytes=max_bytes
                )
            result: dict[str, Any] = {
                "document_id": document_id,
                "filename": download.filename,
                "content_type": download.content_type,
                "size": len(download.content),
            }
            if download.content_type.lower().startswith(TEXT_TYPES):
                try:
                    result["text"] = download.content.decode("utf-8")
                    return result
                except UnicodeDecodeError:
                    pass
            result["base64"] = base64.b64encode(download.content).decode("ascii")
            return result
        except Exception as e:
            await fail(ctx, f"Error downloading document {document_id}", e)
