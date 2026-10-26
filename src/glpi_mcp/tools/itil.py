"""
Tickets, changes and problems: search, read, write, timeline and team tools.
"""

from typing import Annotated
from typing import Any
from typing import Literal

import httpx2
from fastmcp import Context
from pydantic import Field

from glpi_mcp.glpi_client import GlpiClient
from glpi_mcp.models import GlpiConfig
from glpi_mcp.rsql import combine
from glpi_mcp.tools import common
from glpi_mcp.tools.common import CREATE_ANNOTATIONS
from glpi_mcp.tools.common import DELETE_ANNOTATIONS
from glpi_mcp.tools.common import READ_ANNOTATIONS
from glpi_mcp.tools.common import READ_ONLY_TAG
from glpi_mcp.tools.common import UPDATE_ANNOTATIONS
from glpi_mcp.tools.common import FieldsArg
from glpi_mcp.tools.common import FilterArg
from glpi_mcp.tools.common import FiltersArg
from glpi_mcp.tools.common import ForceArg
from glpi_mcp.tools.common import LimitArg
from glpi_mcp.tools.common import SortArg
from glpi_mcp.tools.common import StartArg
from glpi_mcp.tools.common import any_of
from glpi_mcp.tools.common import delete
from glpi_mcp.tools.common import plain_text
from glpi_mcp.tools.common import query_filter
from glpi_mcp.tools.common import search
from glpi_mcp.tools.errors import fail

# Properties returned by glpi_search_itil unless the caller asks for others;
# full items carry the HTML description and cost many more tokens.
SUMMARY_FIELDS = [
    "id",
    "name",
    "type",
    "status",
    "priority",
    "urgency",
    "impact",
    "category",
    "entity",
    "date",
    "date_mod",
    "resolution_date",
]

ItilTypeArg = Annotated[
    Literal["Ticket", "Change", "Problem"],
    Field(default="Ticket", description="ITIL object type."),
]
ItilIdArg = Annotated[int, Field(description="Ticket, change or problem ID.", ge=1)]
ContentArg = Annotated[
    str, Field(description="Text of the entry; plain text or HTML.", min_length=1)
]
IsPrivateArg = Annotated[
    bool,
    Field(default=False, description="Visible to technicians only, not requesters."),
]
OptionalIdArg = Annotated[int | None, Field(default=None, ge=0)]
RawHtmlArg = Annotated[
    bool,
    Field(
        default=False,
        description="Keep descriptions and timeline content as HTML instead of "
        "converting them to plain text.",
    ),
]
LevelArg = Annotated[
    int | None,
    Field(default=None, ge=1, le=5, description="1 (very low) to 5 (very high)."),
]
ExtraArg = Annotated[
    dict[str, Any] | None,
    Field(
        default=None,
        description="More properties as named in the schema (glpi_describe_schema "
        "'Ticket'), merged into the request, e.g. {'request_type': 1}.",
    ),
]
MemberTypeArg = Annotated[
    Literal["User", "Group", "Supplier"], Field(description="Kind of team member.")
]
RoleArg = Annotated[
    Literal["requester", "assigned", "observer"],
    Field(description="Role of the team member."),
]

STATUS_HELP = (
    "Status IDs. Ticket: 1 New, 2 Processing (assigned), 3 Processing (planned), "
    "4 Pending, 5 Solved, 6 Closed. Problem: 1 New, 7 Accepted, 2 Processing "
    "(assigned), 3 Processing (planned), 4 Pending, 5 Solved, 8 Observed, 6 "
    "Closed. Change: 1 New, 9 Evaluation, 10 Approval, 7 Accepted, 4 Pending, 11 "
    "Testing, 12 Qualification, 5 Applied, 8 Review, 6 Closed, 13 Cancelled, 14 "
    "Refused. glpi_describe_schema lists them for this GLPI."
)


def _set(payload: dict[str, Any], key: str, value: Any) -> None:
    """Add ``key`` to a request body unless no value was given."""
    if value is not None:
        payload[key] = value


def register_itil_tools(
    mcp, config: GlpiConfig, transport: httpx2.AsyncBaseTransport | None = None
):
    """Register ticket, change and problem tools with the MCP server"""

    def base(itil_type: str, item_id: int | None = None, *rest: str) -> str:
        parts = ["Assistance", itil_type]
        if item_id is not None:
            parts.append(str(item_id))
        return "/".join([*parts, *rest])

    async def write(
        method: str, endpoint: str, payload: Any, action: str, **extra: Any
    ) -> Any:
        return await common.write(
            config, transport, method, endpoint, payload, action, **extra
        )

    @mcp.tool(
        tags={"glpi", "itil", "tickets", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_search_itil(
        ctx: Context,
        itil_type: ItilTypeArg = "Ticket",
        text: Annotated[
            str | None,
            Field(
                default=None,
                description="Words to find in the title or description "
                "(case-insensitive).",
            ),
        ] = None,
        status: Annotated[
            list[int] | None, Field(default=None, description=STATUS_HELP)
        ] = None,
        exclude_status: Annotated[
            list[int] | None,
            Field(
                default=None,
                description="Status IDs to leave out, e.g. [5, 6] for tickets "
                "that are neither solved nor closed.",
            ),
        ] = None,
        ticket_type: Annotated[
            Literal[1, 2] | None,
            Field(default=None, description="Tickets only: 1 Incident, 2 Request."),
        ] = None,
        priority: Annotated[
            list[int] | None,
            Field(default=None, description="Priorities 1 (very low) to 6 (major)."),
        ] = None,
        category_id: OptionalIdArg = None,
        entity_id: OptionalIdArg = None,
        opened_after: Annotated[
            str | None,
            Field(
                default=None,
                description="Opened on or after this date/time, "
                "e.g. '2026-01-31' or '2026-01-31 08:00:00'.",
            ),
        ] = None,
        opened_before: Annotated[
            str | None,
            Field(default=None, description="Opened on or before this date/time."),
        ] = None,
        filters: FiltersArg = None,
        filter: FilterArg = None,
        sort: SortArg = "date_mod:desc",
        fields: FieldsArg = None,
        start: StartArg = 0,
        limit: LimitArg = 20,
    ) -> dict:
        """Search tickets, changes or problems.

        Returns a compact summary of each item (ID, title, status, priority,
        category, entity, dates) unless 'fields' asks for other properties;
        use glpi_get_itil for the description and glpi_get_itil_timeline for
        the conversation. The team (requesters, assignees) cannot be filtered
        on - GLPI computes it per item; read it with glpi_list_itil_team.
        """
        try:
            clauses: dict[str, Any] = {}
            if status:
                clauses["status.id__in"] = status
            if exclude_status:
                clauses["status.id__out"] = exclude_status
            if ticket_type is not None:
                if itil_type != "Ticket":
                    raise ValueError("'ticket_type' applies to tickets only.")
                clauses["type"] = ticket_type
            if priority:
                clauses["priority__in"] = priority
            if category_id is not None:
                clauses["category.id"] = category_id
            if entity_id is not None:
                clauses["entity.id"] = entity_id
            if opened_after:
                clauses["date__ge"] = opened_after
            if opened_before:
                clauses["date__le"] = opened_before
            rsql = combine(
                [
                    query_filter(clauses, None),
                    any_of(("name", "content"), text),
                    query_filter(filters, filter),
                ]
            )
            return await search(
                config,
                transport,
                base(itil_type),
                rsql,
                sort=sort,
                start=start,
                limit=limit,
                fields=fields or SUMMARY_FIELDS,
            )
        except Exception as e:
            await fail(ctx, f"Error searching {itil_type.lower()}s", e)

    @mcp.tool(
        tags={"glpi", "itil", "tickets", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_get_itil(
        ctx: Context,
        item_id: ItilIdArg,
        itil_type: ItilTypeArg = "Ticket",
        raw_html: RawHtmlArg = False,
    ) -> Any:
        """Get a ticket, change or problem with its description and team."""
        try:
            async with GlpiClient(config, transport=transport) as glpi:
                item = await glpi.get(base(itil_type, item_id))
            return item if raw_html else plain_text(item)
        except Exception as e:
            await fail(ctx, f"Error getting {itil_type.lower()} {item_id}", e)

    @mcp.tool(
        tags={"glpi", "itil", "tickets", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_get_itil_timeline(
        ctx: Context,
        item_id: ItilIdArg,
        itil_type: ItilTypeArg = "Ticket",
        types: Annotated[
            list[Literal["Followup", "Task", "Solution", "Validation", "Document"]]
            | None,
            Field(default=None, description="Only these kinds of entries."),
        ] = None,
        raw_html: RawHtmlArg = False,
    ) -> dict:
        """Get the timeline of a ticket, change or problem.

        Followups, tasks, solutions, approvals and documents in one list, each
        entry as {'type': ..., 'item': {...}}.
        """
        try:
            async with GlpiClient(config, transport=transport) as glpi:
                timeline = await glpi.get(base(itil_type, item_id, "Timeline"))
            entries = timeline if isinstance(timeline, list) else []
            if types:
                wanted = {t.lower() for t in types}
                entries = [
                    entry
                    for entry in entries
                    if isinstance(entry, dict)
                    and str(entry.get("type", "")).lower() in wanted
                ]
            if not raw_html:
                entries = plain_text(entries)
            return {"count": len(entries), "timeline": entries}
        except Exception as e:
            await fail(ctx, f"Error getting the timeline of {itil_type} {item_id}", e)

    @mcp.tool(
        tags={"glpi", "itil", "tickets", "write"},
        annotations=CREATE_ANNOTATIONS,
    )
    async def glpi_create_itil(
        ctx: Context,
        name: Annotated[str, Field(description="Title.", min_length=1)],
        content: ContentArg,
        itil_type: ItilTypeArg = "Ticket",
        ticket_type: Annotated[
            Literal[1, 2] | None,
            Field(default=None, description="Tickets only: 1 Incident, 2 Request."),
        ] = None,
        urgency: LevelArg = None,
        impact: LevelArg = None,
        priority: Annotated[
            int | None,
            Field(
                default=None,
                ge=1,
                le=6,
                description="1 (very low) to 6 (major); computed from urgency "
                "and impact when omitted.",
            ),
        ] = None,
        category_id: OptionalIdArg = None,
        entity_id: OptionalIdArg = None,
        location_id: OptionalIdArg = None,
        extra: ExtraArg = None,
    ) -> Any:
        """Create a ticket, change or problem.

        The new item is created in the current entity unless 'entity_id' is
        given. Add requesters and technicians afterwards with
        glpi_add_itil_team_member.
        """
        try:
            payload: dict[str, Any] = {**(extra or {}), "name": name}
            payload["content"] = content
            if ticket_type is not None:
                if itil_type != "Ticket":
                    raise ValueError("'ticket_type' applies to tickets only.")
                payload["type"] = ticket_type
            _set(payload, "urgency", urgency)
            _set(payload, "impact", impact)
            _set(payload, "priority", priority)
            _set(payload, "category", category_id)
            _set(payload, "entity", entity_id)
            _set(payload, "location", location_id)
            return await write("POST", base(itil_type), payload, "create")
        except Exception as e:
            await fail(ctx, f"Error creating {itil_type.lower()}", e)

    @mcp.tool(
        tags={"glpi", "itil", "tickets", "write"},
        annotations=UPDATE_ANNOTATIONS,
    )
    async def glpi_update_itil(
        ctx: Context,
        item_id: ItilIdArg,
        changes: Annotated[
            dict[str, Any],
            Field(
                description="Properties to change, e.g. {'status': 2, 'priority': "
                "4, 'category': 7, 'name': 'New title'}. See glpi_describe_schema.",
            ),
        ],
        itil_type: ItilTypeArg = "Ticket",
    ) -> Any:
        """Update a ticket, change or problem; only the given properties change.

        To solve an item use glpi_add_itil_solution, which records the
        solution text; setting the status alone does not.
        """
        try:
            if not changes:
                raise ValueError("Give at least one property to change.")
            return await write("PATCH", base(itil_type, item_id), changes, "update")
        except Exception as e:
            await fail(ctx, f"Error updating {itil_type.lower()} {item_id}", e)

    @mcp.tool(
        tags={"glpi", "itil", "tickets", "write"},
        annotations=DELETE_ANNOTATIONS,
    )
    async def glpi_delete_itil(
        ctx: Context,
        item_id: ItilIdArg,
        itil_type: ItilTypeArg = "Ticket",
        force: ForceArg = False,
    ) -> Any:
        """Delete a ticket, change or problem: to the trash, or purged with force."""
        try:
            return await delete(config, transport, base(itil_type, item_id), force)
        except Exception as e:
            await fail(ctx, f"Error deleting {itil_type.lower()} {item_id}", e)

    @mcp.tool(
        tags={"glpi", "itil", "tickets", "write"},
        annotations=CREATE_ANNOTATIONS,
    )
    async def glpi_add_itil_followup(
        ctx: Context,
        item_id: ItilIdArg,
        content: ContentArg,
        itil_type: ItilTypeArg = "Ticket",
        is_private: IsPrivateArg = False,
        request_type_id: OptionalIdArg = None,
    ) -> Any:
        """Add a followup (a comment in the conversation) to a ticket, change or problem."""
        try:
            payload: dict[str, Any] = {"content": content, "is_private": is_private}
            _set(payload, "request_type", request_type_id)
            endpoint = base(itil_type, item_id, "Timeline", "Followup")
            return await write("POST", endpoint, payload, "add followup")
        except Exception as e:
            await fail(ctx, f"Error adding a followup to {itil_type} {item_id}", e)

    @mcp.tool(
        tags={"glpi", "itil", "tickets", "write"},
        annotations=CREATE_ANNOTATIONS,
    )
    async def glpi_add_itil_task(
        ctx: Context,
        item_id: ItilIdArg,
        content: ContentArg,
        itil_type: ItilTypeArg = "Ticket",
        is_private: IsPrivateArg = False,
        state: Annotated[
            Literal[0, 1, 2] | None,
            Field(default=None, description="0 Information, 1 To do, 2 Done."),
        ] = None,
        duration: Annotated[
            int | None,
            Field(default=None, ge=0, description="Time spent, in seconds."),
        ] = None,
        category_id: OptionalIdArg = None,
        user_tech_id: Annotated[
            int | None,
            Field(default=None, ge=1, description="Technician user in charge."),
        ] = None,
        group_tech_id: Annotated[
            int | None,
            Field(default=None, ge=1, description="Technician group in charge."),
        ] = None,
        planned_begin: Annotated[
            str | None,
            Field(
                default=None, description="Planned start, e.g. '2026-02-01T09:00:00Z'."
            ),
        ] = None,
        planned_end: Annotated[
            str | None, Field(default=None, description="Planned end.")
        ] = None,
    ) -> Any:
        """Add a task (work to do or done, with time spent) to a ticket, change or problem."""
        try:
            payload: dict[str, Any] = {"content": content, "is_private": is_private}
            _set(payload, "state", state)
            _set(payload, "duration", duration)
            _set(payload, "category", category_id)
            _set(payload, "user_tech", user_tech_id)
            _set(payload, "group_tech", group_tech_id)
            _set(payload, "planned_begin", planned_begin)
            _set(payload, "planned_end", planned_end)
            endpoint = base(itil_type, item_id, "Timeline", "Task")
            return await write("POST", endpoint, payload, "add task")
        except Exception as e:
            await fail(ctx, f"Error adding a task to {itil_type} {item_id}", e)

    @mcp.tool(
        tags={"glpi", "itil", "tickets", "write"},
        annotations=CREATE_ANNOTATIONS,
    )
    async def glpi_add_itil_solution(
        ctx: Context,
        item_id: ItilIdArg,
        content: ContentArg,
        itil_type: ItilTypeArg = "Ticket",
        solution_type_id: OptionalIdArg = None,
    ) -> Any:
        """Add a solution to a ticket, change or problem, which marks it solved.

        Depending on GLPI's settings the requester may still have to approve it.
        """
        try:
            payload: dict[str, Any] = {"content": content}
            _set(payload, "type", solution_type_id)
            endpoint = base(itil_type, item_id, "Timeline", "Solution")
            return await write("POST", endpoint, payload, "add solution")
        except Exception as e:
            await fail(ctx, f"Error adding a solution to {itil_type} {item_id}", e)

    @mcp.tool(
        tags={"glpi", "itil", "tickets", "write"},
        annotations=CREATE_ANNOTATIONS,
    )
    async def glpi_request_itil_validation(
        ctx: Context,
        item_id: ItilIdArg,
        approver_id: Annotated[int, Field(ge=1, description="Approver's ID.")],
        itil_type: Annotated[
            Literal["Ticket", "Change"],
            Field(
                default="Ticket", description="Only tickets and changes have approvals."
            ),
        ] = "Ticket",
        approver_type: Annotated[
            Literal["User", "Group"],
            Field(
                default="User", description="Whether approver_id is a user or a group."
            ),
        ] = "User",
        comment: Annotated[
            str | None, Field(default=None, description="Message to the approver.")
        ] = None,
    ) -> Any:
        """Ask a user or group to approve a ticket or change."""
        try:
            payload: dict[str, Any] = {
                "requested_approver_type": approver_type,
                "requested_approver_id": approver_id,
            }
            _set(payload, "submission_comment", comment)
            endpoint = base(itil_type, item_id, "Timeline", "Validation")
            return await write("POST", endpoint, payload, "request validation")
        except Exception as e:
            await fail(ctx, f"Error requesting approval of {itil_type} {item_id}", e)

    @mcp.tool(
        tags={"glpi", "itil", "tickets", READ_ONLY_TAG},
        annotations=READ_ANNOTATIONS,
    )
    async def glpi_list_itil_team(
        ctx: Context,
        item_id: ItilIdArg,
        itil_type: ItilTypeArg = "Ticket",
        role: Annotated[
            Literal["requester", "assigned", "observer"] | None,
            Field(default=None, description="Only members with this role."),
        ] = None,
    ) -> Any:
        """List the requesters, assigned technicians/groups and observers of an item."""
        try:
            rest = ["TeamMember", *([role] if role else [])]
            async with GlpiClient(config, transport=transport) as glpi:
                return {"team": await glpi.get(base(itil_type, item_id, *rest))}
        except Exception as e:
            await fail(ctx, f"Error listing the team of {itil_type} {item_id}", e)

    @mcp.tool(
        tags={"glpi", "itil", "tickets", "write"},
        annotations=CREATE_ANNOTATIONS,
    )
    async def glpi_add_itil_team_member(
        ctx: Context,
        item_id: ItilIdArg,
        member_type: MemberTypeArg,
        member_id: Annotated[
            int, Field(ge=1, description="User, group or supplier ID.")
        ],
        role: RoleArg,
        itil_type: ItilTypeArg = "Ticket",
    ) -> Any:
        """Add a requester, assignee or observer (user, group or supplier) to an item.

        Assigning a technician: member_type='User', role='assigned'.
        """
        try:
            payload = {"type": member_type, "id": member_id, "role": role}
            endpoint = base(itil_type, item_id, "TeamMember")
            return await write("POST", endpoint, payload, "add team member")
        except Exception as e:
            await fail(ctx, f"Error adding a team member to {itil_type} {item_id}", e)

    @mcp.tool(
        tags={"glpi", "itil", "tickets", "write"},
        annotations=DELETE_ANNOTATIONS,
    )
    async def glpi_remove_itil_team_member(
        ctx: Context,
        item_id: ItilIdArg,
        member_type: MemberTypeArg,
        member_id: Annotated[
            int, Field(ge=1, description="User, group or supplier ID.")
        ],
        role: RoleArg,
        itil_type: ItilTypeArg = "Ticket",
    ) -> Any:
        """Remove a requester, assignee or observer from a ticket, change or problem."""
        try:
            payload = {"type": member_type, "id": member_id, "role": role}
            endpoint = base(itil_type, item_id, "TeamMember")
            result = await write("DELETE", endpoint, payload, "remove team member")
            return result if config.dry_run_mode else {"removed": True, **payload}
        except Exception as e:
            await fail(
                ctx, f"Error removing a team member from {itil_type} {item_id}", e
            )
