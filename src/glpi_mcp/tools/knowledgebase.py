"""
Knowledge base tools.

GLPI 12's API 3.0 replaced KB categories with parent articles: an article's
``categories`` became ``parents`` and ``/Knowledgebase/Category`` was removed.
The tools take whichever the API version in use has.
"""

from typing import Annotated
from typing import Any

import httpx2
from fastmcp import Context
from pydantic import Field

from glpi_mcp.glpi_client import GlpiClient
from glpi_mcp.models import GlpiConfig
from glpi_mcp.rsql import combine
from glpi_mcp.tools.common import CREATE_ANNOTATIONS
from glpi_mcp.tools.common import READ_ANNOTATIONS
from glpi_mcp.tools.common import READ_ONLY_TAG
from glpi_mcp.tools.common import UPDATE_ANNOTATIONS
from glpi_mcp.tools.common import FieldsArg
from glpi_mcp.tools.common import FilterArg
from glpi_mcp.tools.common import FiltersArg
from glpi_mcp.tools.common import ItemIdArg
from glpi_mcp.tools.common import LimitArg
from glpi_mcp.tools.common import SortArg
from glpi_mcp.tools.common import StartArg
from glpi_mcp.tools.common import any_of
from glpi_mcp.tools.common import created
from glpi_mcp.tools.common import dry_run_result
from glpi_mcp.tools.common import page
from glpi_mcp.tools.common import plain_text
from glpi_mcp.tools.common import query_filter
from glpi_mcp.tools.common import search
from glpi_mcp.tools.common import write
from glpi_mcp.tools.errors import fail

ARTICLES = "Knowledgebase/Article"
CATEGORIES = "Knowledgebase/Category"
# API version that dropped KB categories for parent articles.
PARENTS_VERSION = "3.0"
ARTICLE_SUMMARY_FIELDS = ["id", "name", "description", "is_faq", "date_mod"]

RawHtmlArg = Annotated[
    bool,
    Field(default=False, description="Keep the article body as HTML."),
]
ParentIdsArg = Annotated[
    list[int] | None,
    Field(
        default=None,
        description="Categories (API 2.3, GLPI 11) or parent articles (API 3.0, "
        "GLPI 12) to file the article under.",
    ),
]


async def hierarchy_property(glpi: GlpiClient) -> str:
    """The article property that files it in the KB tree for this API version."""
    return "parents" if await glpi.api_version() == PARENTS_VERSION else "categories"


def register_knowledgebase_tools(
    mcp, config: GlpiConfig, transport: httpx2.AsyncBaseTransport | None = None
):
    """Register knowledge base tools with the MCP server"""

    @mcp.tool(
        tags={"glpi", "knowledgebase", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_search_kb_articles(
        ctx: Context,
        text: Annotated[
            str | None,
            Field(
                default=None,
                description="Words to find in the title or body (case-insensitive).",
            ),
        ] = None,
        faq_only: Annotated[
            bool, Field(default=False, description="Only FAQ articles.")
        ] = False,
        filters: FiltersArg = None,
        filter: FilterArg = None,
        sort: SortArg = None,
        fields: FieldsArg = None,
        start: StartArg = 0,
        limit: LimitArg = 20,
    ) -> dict:
        """Search knowledge base articles, e.g. for a known fix to a reported problem.

        Returns titles and summaries; read the body with glpi_get_kb_article.
        """
        try:
            rsql = combine(
                [
                    any_of(("name", "content"), text),
                    "is_faq==1" if faq_only else None,
                    query_filter(filters, filter),
                ]
            )
            return await search(
                config,
                transport,
                ARTICLES,
                rsql,
                sort=sort,
                start=start,
                limit=limit,
                fields=fields or ARTICLE_SUMMARY_FIELDS,
            )
        except Exception as e:
            await fail(ctx, "Error searching the knowledge base", e)

    @mcp.tool(
        tags={"glpi", "knowledgebase", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_get_kb_article(
        ctx: Context,
        article_id: ItemIdArg,
        raw_html: RawHtmlArg = False,
    ) -> Any:
        """Get a knowledge base article with its full body."""
        try:
            async with GlpiClient(config, transport=transport) as glpi:
                article = await glpi.get(f"{ARTICLES}/{article_id}")
            return article if raw_html else plain_text(article)
        except Exception as e:
            await fail(ctx, f"Error getting KB article {article_id}", e)

    @mcp.tool(
        tags={"glpi", "knowledgebase", "write"},
        annotations=CREATE_ANNOTATIONS,
    )
    async def glpi_create_kb_article(
        ctx: Context,
        name: Annotated[str, Field(min_length=1, description="Title.")],
        content: Annotated[
            str, Field(min_length=1, description="Body; plain text or HTML.")
        ],
        is_faq: Annotated[
            bool, Field(default=False, description="Publish in the FAQ.")
        ] = False,
        filed_under: ParentIdsArg = None,
        entity_id: Annotated[int | None, Field(default=None, ge=0)] = None,
        extra: Annotated[
            dict[str, Any] | None,
            Field(
                default=None,
                description="More properties (glpi_describe_schema 'KBArticle').",
            ),
        ] = None,
    ) -> Any:
        """Create a knowledge base article."""
        try:
            payload: dict[str, Any] = {
                **(extra or {}),
                "name": name,
                "content": content,
                "is_faq": is_faq,
            }
            if entity_id is not None:
                payload["entity"] = entity_id
            async with GlpiClient(config, transport=transport) as glpi:
                if filed_under:
                    payload[await hierarchy_property(glpi)] = [
                        {"id": i} for i in filed_under
                    ]
                if config.dry_run_mode:
                    return dry_run_result("create", "POST", ARTICLES, payload)
                return created(await glpi.post(ARTICLES, json=payload), ARTICLES)
        except Exception as e:
            await fail(ctx, "Error creating KB article", e)

    @mcp.tool(
        tags={"glpi", "knowledgebase", "write"},
        annotations=UPDATE_ANNOTATIONS,
    )
    async def glpi_update_kb_article(
        ctx: Context,
        article_id: ItemIdArg,
        changes: Annotated[
            dict[str, Any],
            Field(description="Properties to change, e.g. {'content': '...'}."),
        ],
    ) -> Any:
        """Update a knowledge base article; only the given properties change."""
        try:
            endpoint = f"{ARTICLES}/{article_id}"
            return await write(config, transport, "PATCH", endpoint, changes, "update")
        except Exception as e:
            await fail(ctx, f"Error updating KB article {article_id}", e)

    @mcp.tool(
        tags={"glpi", "knowledgebase", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_list_kb_categories(
        ctx: Context,
        start: StartArg = 0,
        limit: LimitArg = 100,
    ) -> dict:
        """List knowledge base categories (GLPI 11 / API 2.3).

        On API 3.0 (GLPI 12) categories no longer exist: articles are filed under
        parent articles instead, so this lists the top-level articles.
        """
        try:
            async with GlpiClient(config, transport=transport) as glpi:
                if await glpi.api_version() == PARENTS_VERSION:
                    result = await glpi.collection(
                        ARTICLES, filter="parents=empty=", start=start, limit=limit
                    )
                    envelope = page(ARTICLES, result, ["id", "name"])
                    envelope["note"] = (
                        "API 3.0 has no KB categories; these are top-level "
                        "articles, which other articles can be filed under."
                    )
                    return envelope
                result = await glpi.collection(CATEGORIES, start=start, limit=limit)
            result.rows = plain_text(result.rows)
            return page(CATEGORIES, result)
        except Exception as e:
            await fail(ctx, "Error listing KB categories", e)
