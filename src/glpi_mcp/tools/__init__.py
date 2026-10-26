"""
GLPI MCP Server Tools package
"""

import httpx2

from glpi_mcp.models import GlpiConfig
from glpi_mcp.tools.administration import register_administration_tools
from glpi_mcp.tools.assets import register_asset_tools
from glpi_mcp.tools.documents import register_document_tools
from glpi_mcp.tools.generic import register_generic_tools
from glpi_mcp.tools.itil import register_itil_tools
from glpi_mcp.tools.knowledgebase import register_knowledgebase_tools
from glpi_mcp.tools.session import register_session_tools


def register_tools(
    mcp, config: GlpiConfig, transport: httpx2.AsyncBaseTransport | None = None
):
    """Register all GLPI tools with the MCP server.

    Args:
        mcp: The FastMCP server.
        config: GLPI connection settings.
        transport: Transport for every GLPI request. None uses the shared
            network transport; tests pass an httpx2.MockTransport.
    """
    register_session_tools(mcp, config, transport)
    register_generic_tools(mcp, config, transport)
    register_itil_tools(mcp, config, transport)
    register_asset_tools(mcp, config, transport)
    register_administration_tools(mcp, config, transport)
    register_knowledgebase_tools(mcp, config, transport)
    register_document_tools(mcp, config, transport)
