"""
Shared argument types, annotations and response helpers for GLPI tools.
"""

import html
import re
from collections.abc import Iterable
from html.parser import HTMLParser
from typing import Annotated
from typing import Any

import httpx2
from pydantic import Field

from glpi_mcp.glpi_client import GlpiClient
from glpi_mcp.glpi_client import Page
from glpi_mcp.models import GlpiConfig
from glpi_mcp.rsql import build_filter
from glpi_mcp.rsql import combine

READ_ONLY_TAG = "read-only"

READ_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}
CREATE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": True,
}
UPDATE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}
DELETE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}

# Endpoints a generic tool must never reach: they hand out or mint credentials.
FORBIDDEN_PATHS = frozenset({"token", "authorize", "swagger-oauth-redirect"})
PATH_SEGMENT = re.compile(r"^[A-Za-z0-9_\-]+$")

ItemIdArg = Annotated[int, Field(description="Numeric item ID.", ge=1)]
FilterArg = Annotated[
    str | None,
    Field(
        default=None,
        description="Raw RSQL filter, e.g. 'name=ilike=*printer*;status.id=in=(1,2)'. "
        "Operators: ==, !=, =in=, =out=, =lt=, =le=, =gt=, =ge=, =like=, =ilike= "
        "(* is the wildcard), =isnull=, =isnotnull=, =empty=, =notempty=. ';' is "
        "AND, ',' is OR. AND-ed with 'filters'.",
    ),
]
FiltersArg = Annotated[
    dict[str, Any] | None,
    Field(
        default=None,
        description="Filters as {'property': value}, AND-ed, e.g. {'status.id': "
        "[1, 2], 'name__ilike': '*srv*', 'entity.id': 0}. Use dot paths for "
        "nested properties. Suffixes: __ne, __in, __out, __lt, __le, __gt, __ge, "
        "__like, __ilike, __isnull, __isnotnull, __empty, __notempty. A list "
        "means 'any of'; None means 'is null'.",
    ),
]
SortArg = Annotated[
    str | None,
    Field(
        default=None,
        description="Sort order as 'property:asc' or 'property:desc', comma-"
        "separated, e.g. 'date_mod:desc,id:asc'.",
    ),
]
FieldsArg = Annotated[
    list[str] | None,
    Field(
        default=None,
        description="Only return these top-level properties, e.g. ['id', 'name', "
        "'status']. Pass this for listings - full items cost many more tokens.",
    ),
]
StartArg = Annotated[
    int, Field(default=0, ge=0, description="Index of the first result (offset).")
]
LimitArg = Annotated[
    int, Field(default=20, ge=1, le=1000, description="Page size (1-1000).")
]
ForceArg = Annotated[
    bool,
    Field(
        default=False,
        description="Purge permanently instead of moving to the trash. Items "
        "without a trash are always purged.",
    ),
]


def query_filter(filters: dict[str, Any] | None, filter: str | None) -> str | None:
    """Combine structured filters and a raw RSQL filter into one."""
    return combine([build_filter(filters), filter])


def any_of(props: Iterable[str], text: str | None) -> str | None:
    """RSQL matching ``text`` in any of ``props`` (case-insensitive)."""
    if not text:
        return None
    term = text.replace("*", "")
    return ",".join(
        build_filter({f"{prop}__ilike": f"*{term}*"}) or "" for prop in props
    )


async def search(
    config: GlpiConfig,
    transport: httpx2.AsyncBaseTransport | None,
    endpoint: str,
    rsql: str | None,
    *,
    sort: str | None,
    start: int,
    limit: int,
    fields: list[str] | None,
    raw_html: bool = False,
) -> dict:
    """Fetch one page of ``endpoint`` and wrap it in the list-tool envelope."""
    async with GlpiClient(config, transport=transport) as glpi:
        result = await glpi.collection(
            endpoint, filter=rsql, sort=sort, start=start, limit=limit
        )
    # Trimmed first, so rich text that 'fields' leaves out is never converted.
    result.rows = project(result.rows, fields)
    if not raw_html:
        result.rows = plain_text(result.rows)
    return page(endpoint, result)


async def write(
    config: GlpiConfig,
    transport: httpx2.AsyncBaseTransport | None,
    method: str,
    endpoint: str,
    payload: Any,
    action: str,
    **extra: Any,
) -> Any:
    """Send a write (POST/PATCH/DELETE with a body), honoring DRY_RUN_MODE.

    Returns:
        Any: The simulated request in dry-run mode, the ``created`` envelope
            for a POST, or GLPI's decoded answer otherwise.
    """
    if config.dry_run_mode:
        return dry_run_result(action, method, endpoint, payload, **extra)
    async with GlpiClient(config, transport=transport) as glpi:
        result = await glpi.request(method, endpoint, json=payload)
    return created(result, endpoint) if method == "POST" else result


async def delete(
    config: GlpiConfig,
    transport: httpx2.AsyncBaseTransport | None,
    endpoint: str,
    force: bool,
) -> dict:
    """Delete (or with ``force`` purge) an item, honoring DRY_RUN_MODE."""
    params = {"force": "true"} if force else None
    if config.dry_run_mode:
        return dry_run_result(
            "purge" if force else "delete", "DELETE", endpoint, params
        )
    async with GlpiClient(config, transport=transport) as glpi:
        await glpi.delete(endpoint, params=params)
    return {"deleted": True, "purged": force, "endpoint": endpoint}


def pick(obj: Any, keys: Iterable[str]) -> Any:
    """Keep only ``keys`` of a dict, in that order; anything else passes through."""
    if not isinstance(obj, dict):
        return obj
    return {key: obj[key] for key in keys if key in obj}


def project(rows: list[Any], fields: list[str] | None) -> list[Any]:
    """Trim rows to the requested top-level properties.

    The High-Level API always returns full items, so trimming happens here to
    keep responses small.
    """
    if not fields:
        return rows
    return [pick(row, fields) for row in rows]


def page(endpoint: str, result: Page, fields: list[str] | None = None) -> dict:
    """Wrap a page of results in the envelope every list tool returns.

    Returns:
        dict: ``results`` plus ``count`` (this page), ``total`` (all matches,
            when GLPI reports it), ``start``, ``limit``, ``has_more`` and
            ``next_start``.
    """
    envelope = {
        "endpoint": endpoint,
        "results": project(result.rows, fields),
        "count": len(result.rows),
        "total": result.total,
        "start": result.start,
        "limit": result.limit,
        "has_more": result.has_more,
    }
    if result.has_more:
        envelope["next_start"] = result.start + len(result.rows)
    return envelope


def dry_run_result(
    action: str, method: str, endpoint: str, payload: Any, **extra: Any
) -> dict:
    """Describe a write that DRY_RUN_MODE stopped from being sent.

    Args:
        action: What the tool would do, e.g. ``"create"``.
        method: HTTP method that would be used.
        endpoint: API endpoint that would be called.
        payload: Request body that would be sent.
        **extra: Tool-specific context, e.g. the current item state.

    Returns:
        dict: The simulated request, flagged ``dry_run: True``.
    """
    return {
        "dry_run": True,
        "message": "DRY_RUN_MODE is enabled - nothing was changed in GLPI. "
        "This is the request that would have been sent.",
        "action": action,
        "method": method,
        "endpoint": endpoint,
        "payload": payload,
        **extra,
    }


def created(result: Any, endpoint: str) -> dict:
    """Describe a 201 answer: GLPI returns the new item's ID and link."""
    if isinstance(result, dict):
        return {"created": True, "endpoint": endpoint, **result}
    return {"created": True, "endpoint": endpoint, "result": result}


def api_path(*segments: str | int) -> str:
    """Join path segments, rejecting anything that could leave the API.

    Generic tools take endpoint paths from the model, so a path must not climb
    out with ``..``, carry a query string, or name a credential endpoint.

    Raises:
        ValueError: If a segment is not a plain name or number.
    """
    parts: list[str] = []
    for segment in segments:
        for part in str(segment).strip("/").split("/"):
            if not part:
                continue
            if not PATH_SEGMENT.match(part):
                raise ValueError(
                    f"Invalid path segment {part!r}: use names such as "
                    "'Assistance/Ticket' (glpi_list_endpoints lists them)."
                )
            parts.append(part)
    if not parts:
        raise ValueError("An endpoint path is required, e.g. 'Assistance/Ticket'.")
    if parts[0].lower() in FORBIDDEN_PATHS:
        raise ValueError(f"The {parts[0]!r} endpoint is not available through tools.")
    return "/".join(parts)


# A tag of the kind GLPI's rich-text editor writes. Without one, a value is plain
# text, where "<john@example.com>" must not be parsed (and dropped) as a tag.
HTML_TAG = re.compile(
    r"</?(?:p|div|br|span|ul|ol|li|table|thead|tbody|tr|td|th|h[1-6]|pre|code|a|"
    r"img|strong|em|b|i|u|s|blockquote|hr|font|sub|sup|html|head|body|style|"
    r"center|small|big|del|ins|dl|dt|dd|caption|tfoot|colgroup|col|o:p)"
    r"(?=[\s/>])[^<>]*>",
    re.IGNORECASE,
)


# Stand-ins for whitespace inside <pre>, which the line clean-up in
# html_to_text must not strip: code and config snippets need their indentation.
_PRE_WHITESPACE = {"\n": "\ue000", " ": "\ue001", "\t": "\ue002"}
_PRE_RESTORE = str.maketrans({v: k for k, v in _PRE_WHITESPACE.items()})
_PRE_PROTECT = str.maketrans(_PRE_WHITESPACE)


class _TextExtractor(HTMLParser):
    """Collect the text of an HTML fragment, keeping line structure."""

    BLOCKS = frozenset(
        {
            "p",
            "div",
            "br",
            "li",
            "tr",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "pre",
            "ul",
            "ol",
            "table",
            "blockquote",
            "hr",
        }
    )
    # Table cells, kept apart so "Name" and "John" do not become "NameJohn".
    CELLS = frozenset({"td", "th"})
    # Elements whose content is code or metadata, not text: mail-collector
    # tickets carry whole <style> sheets that would otherwise read as text.
    # Not <head>: mail HTML often leaves it unclosed, which would hide the body.
    HIDDEN = frozenset({"style", "script", "title"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0
        self._pre = 0

    def handle_starttag(self, tag, attrs):  # noqa: ARG002 - fixed signature
        if tag == "pre":
            self._pre += 1
        if tag in self.HIDDEN:
            self._hidden += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")
        elif tag in self.CELLS:
            self.parts.append("\t")
        if tag == "li":
            self.parts.append("- ")

    def handle_startendtag(self, tag, attrs):
        # A self-closing <br/> is one line break, like <br>, not a start and
        # an end tag (which would read as a paragraph break).
        if tag not in self.HIDDEN:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "pre":
            self._pre = max(self._pre - 1, 0)
        if tag in self.HIDDEN:
            self._hidden = max(self._hidden - 1, 0)
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._hidden:
            return
        self.parts.append(data.translate(_PRE_PROTECT) if self._pre else data)


def html_to_text(value: Any) -> Any:
    """Turn GLPI's HTML rich text into plain text; other values pass through.

    Ticket descriptions, followups and KB answers are stored as HTML (sometimes
    entity-encoded twice), which costs tokens and adds nothing for a model.
    """
    if not isinstance(value, str) or ("<" not in value and "&" not in value):
        return value
    text = value
    # GLPI 10-era content may be stored entity-encoded (&lt;p&gt;).
    if "&lt;" in text and "<" not in text:
        text = html.unescape(text)
    if not HTML_TAG.search(text):
        # Plain text (an asset comment, say), possibly with an entity or two.
        return html.unescape(text)
    parser = _TextExtractor()
    parser.feed(text)
    parser.close()
    lines = [line.strip() for line in "".join(parser.parts).splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text.translate(_PRE_RESTORE)


# Properties that hold HTML rich text in GLPI schemas.
RICH_TEXT_FIELDS = ("content", "answer", "comment", "description")


def plain_text(obj: Any, keys: Iterable[str] = RICH_TEXT_FIELDS) -> Any:
    """Convert the rich-text properties of an item (or a list of them) to text."""
    if isinstance(obj, list):
        return [plain_text(item, keys) for item in obj]
    if isinstance(obj, dict):
        result = dict(obj)
        for key in keys:
            if key in result:
                result[key] = html_to_text(result[key])
        # Timeline entries wrap the item in "item".
        if isinstance(result.get("item"), dict):
            result["item"] = plain_text(result["item"], keys)
        return result
    return obj
