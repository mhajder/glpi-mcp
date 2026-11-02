import base64
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from glpi_mcp.tools.common import html_to_text
from tests.conftest import API_VERSIONS_12
from tests.conftest import FakeGlpi
from tests.conftest import build_server

READ_TOOLS = {
    "glpi_server_info",
    "glpi_whoami",
    "glpi_get_entity_tree",
    "glpi_list_endpoints",
    "glpi_describe_schema",
    "glpi_list_items",
    "glpi_get_item",
    "glpi_graphql_query",
    "glpi_search_itil",
    "glpi_get_itil",
    "glpi_get_itil_timeline",
    "glpi_list_itil_team",
    "glpi_list_asset_types",
    "glpi_search_assets",
    "glpi_get_asset",
    "glpi_search_users",
    "glpi_get_user",
    "glpi_search_groups",
    "glpi_get_group",
    "glpi_search_entities",
    "glpi_get_entity",
    "glpi_list_profiles",
    "glpi_search_kb_articles",
    "glpi_get_kb_article",
    "glpi_list_kb_categories",
    "glpi_search_documents",
    "glpi_get_document",
    "glpi_download_document",
}
WRITE_TOOLS = {
    "glpi_create_item",
    "glpi_update_item",
    "glpi_delete_item",
    "glpi_create_itil",
    "glpi_update_itil",
    "glpi_delete_itil",
    "glpi_add_itil_followup",
    "glpi_add_itil_task",
    "glpi_add_itil_solution",
    "glpi_request_itil_validation",
    "glpi_add_itil_team_member",
    "glpi_remove_itil_team_member",
    "glpi_create_asset",
    "glpi_update_asset",
    "glpi_delete_asset",
    "glpi_create_kb_article",
    "glpi_update_kb_article",
}

OPENAPI = {
    "paths": {
        "/Assets/Computer": {"get": {"description": "List computers"}, "post": {}},
        "/Assets/Computer/{id}": {"get": {}, "patch": {}, "delete": {}},
        "/Assets/Monitor": {"get": {}},
        "/Assets/Global": {"get": {}},
        "/Assets/Custom": {"get": {}},
        "/Assets/Custom/Badge": {"get": {}},
        "/Assistance/Ticket": {"get": {"summary": "List tickets"}},
    },
    "components": {
        "schemas": {
            "Ticket": {
                "type": "object",
                "x-itemtype": "Ticket",
                "properties": {
                    "id": {"type": "integer", "readOnly": True},
                    "status": {
                        "type": "object",
                        "properties": {"id": {"type": "integer", "enum": [1, 2]}},
                    },
                },
            },
            "Computer": {"type": "object", "properties": {}},
        }
    },
}


async def test_all_tools_listed(config, client_for):
    async with client_for(config) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == READ_TOOLS | WRITE_TOOLS


async def test_read_only_hides_write_tools(config, client_for):
    async with client_for(config, read_only=True) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == READ_TOOLS


async def test_read_tools_are_annotated_read_only(config, client_for):
    async with client_for(config) as client:
        tools = await client.list_tools()
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint == (tool.name in READ_TOOLS), tool.name


async def test_server_info(config):
    glpi = FakeGlpi(API_VERSIONS_12, version="3.0")
    async with Client(build_server(config, glpi.transport)) as client:
        result = await client.call_tool("glpi_server_info", {})
    assert result.data["api_version"] == "3.0"
    assert [v["version"] for v in result.data["api_versions"]] == [
        "1.0.0",
        "2.3.0",
        "3.0.0",
    ]
    assert result.data["auth_mode"].startswith("password-grant")


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (
            "<p>Run:</p><pre>def f():\n    return 1\n\n\n\tdone</pre><p>End</p>",
            "Run:\n\ndef f():\n    return 1\n\n\n\tdone\n\nEnd",
        ),
        ("<pre><code>  key:\n    value</code></pre>", "  key:\n    value"),
        ("<p>  outside   </p><pre>  inside</pre>", "outside\n\n  inside"),
    ],
)
def test_html_to_text_keeps_pre_formatting(html, expected):
    assert html_to_text(html) == expected


async def test_whoami_drops_the_rights_table(config, client_for, glpi):
    glpi.get(
        "session",
        payload={
            "user_id": 2,
            "active_profile": {"id": 4, "name": "Super-Admin", "rights": {"x": 1}},
        },
    )
    glpi.get("Administration/User/Me", payload={"id": 2})
    async with client_for(config) as client:
        result = await client.call_tool("glpi_whoami", {})
    assert result.data["session"]["active_profile"] == {"id": 4, "name": "Super-Admin"}


async def test_get_asset_without_infocom(config, client_for, glpi):
    glpi.get("Assets/Computer/3", payload={"id": 3})
    glpi.get(
        "Assets/Computer/3/Infocom",
        status=404,
        payload={"status": "ERROR_ITEM_NOT_FOUND"},
    )
    async with client_for(config) as client:
        result = await client.call_tool(
            "glpi_get_asset",
            {"asset_type": "Computer", "asset_id": 3, "include_infocom": True},
        )
    assert result.data == {"id": 3, "infocom": None}


async def test_whoami_without_user_scope(config, client_for, glpi):
    glpi.get("session", payload={"user_id": 2, "name": "glpi"})
    glpi.get("Administration/User/Me", status=403, payload={"status": "ERROR"})
    async with client_for(config) as client:
        result = await client.call_tool("glpi_whoami", {})
    assert result.data == {"session": {"user_id": 2, "name": "glpi"}, "user": None}


async def test_search_itil_builds_the_filter(config, client_for, glpi):
    glpi.get(
        "Assistance/Ticket",
        payload=[{"id": 1, "name": "Printer", "content": "<p>long</p>", "status": {}}],
        headers={"Content-Range": "0-0/1"},
    )
    async with client_for(config) as client:
        result = await client.call_tool(
            "glpi_search_itil",
            {
                "text": "printer jam",
                "exclude_status": [5, 6],
                "ticket_type": 1,
                "entity_id": 0,
                "filters": {"team.id": 5},
            },
        )
    [request] = glpi.sent("GET", "Assistance/Ticket")
    assert request.param("filter") == (
        "status.id=out=(5,6);type==1;entity.id==0;"
        '(name=ilike="*printer jam*",content=ilike="*printer jam*");team.id==5'
    )
    assert request.param("sort") == "date_mod:desc"
    # The summary leaves the HTML description out.
    assert result.data["results"] == [{"id": 1, "name": "Printer", "status": {}}]
    assert result.data["total"] == 1
    assert result.data["has_more"] is False


async def test_search_itil_ticket_type_only_for_tickets(config, client_for, glpi):
    async with client_for(config) as client:
        with pytest.raises(ToolError, match="tickets only"):
            await client.call_tool(
                "glpi_search_itil", {"itil_type": "Change", "ticket_type": 1}
            )
    assert glpi.api_requests() == []


async def test_get_itil_converts_html(config, client_for, glpi):
    glpi.get(
        "Assistance/Problem/3",
        payload={"id": 3, "content": "<p>Line one</p><ul><li>a</li></ul>"},
    )
    async with client_for(config) as client:
        result = await client.call_tool(
            "glpi_get_itil", {"itil_type": "Problem", "item_id": 3}
        )
    assert result.data["content"] == "Line one\n\n- a"


async def test_timeline_filters_types(config, client_for, glpi):
    glpi.get(
        "Assistance/Ticket/1/Timeline",
        payload=[
            {"type": "Followup", "item": {"content": "<b>hi</b>"}},
            {"type": "Task", "item": {"content": "work"}},
        ],
    )
    async with client_for(config) as client:
        result = await client.call_tool(
            "glpi_get_itil_timeline", {"item_id": 1, "types": ["Followup"]}
        )
    assert result.data == {
        "count": 1,
        "timeline": [{"type": "Followup", "item": {"content": "hi"}}],
    }


async def test_create_ticket(config, client_for, glpi):
    glpi.post(
        "Assistance/Ticket",
        status=201,
        payload={"id": 42, "href": "/Assistance/Ticket/42"},
    )
    async with client_for(config) as client:
        result = await client.call_tool(
            "glpi_create_itil",
            {
                "name": "Printer jam",
                "content": "Paper stuck",
                "ticket_type": 1,
                "urgency": 4,
                "category_id": 7,
                "extra": {"request_type": 1},
            },
        )
    assert result.data["id"] == 42
    assert result.data["created"] is True
    [request] = glpi.sent("POST", "Assistance/Ticket")
    assert request.body == {
        "request_type": 1,
        "name": "Printer jam",
        "content": "Paper stuck",
        "type": 1,
        "urgency": 4,
        "category": 7,
    }


async def test_add_followup_and_task(config, client_for, glpi):
    glpi.post("Assistance/Ticket/1/Timeline/Followup", status=201, payload={"id": 9})
    glpi.post("Assistance/Ticket/1/Timeline/Task", status=201, payload={"id": 10})
    async with client_for(config) as client:
        await client.call_tool(
            "glpi_add_itil_followup",
            {"item_id": 1, "content": "Looking", "is_private": True},
        )
        await client.call_tool(
            "glpi_add_itil_task",
            {"item_id": 1, "content": "Replaced", "state": 2, "duration": 900},
        )
    assert glpi.sent("POST", "Assistance/Ticket/1/Timeline/Followup")[0].body == {
        "content": "Looking",
        "is_private": True,
    }
    assert glpi.sent("POST", "Assistance/Ticket/1/Timeline/Task")[0].body == {
        "content": "Replaced",
        "is_private": False,
        "state": 2,
        "duration": 900,
    }


async def test_team_member_add_and_remove(config, client_for, glpi):
    glpi.post("Assistance/Change/2/TeamMember", status=201)
    glpi.delete("Assistance/Change/2/TeamMember", status=200)
    args = {
        "itil_type": "Change",
        "item_id": 2,
        "member_type": "Group",
        "member_id": 4,
        "role": "assigned",
    }
    async with client_for(config) as client:
        await client.call_tool("glpi_add_itil_team_member", args)
        result = await client.call_tool("glpi_remove_itil_team_member", args)
    expected = {"type": "Group", "id": 4, "role": "assigned"}
    assert glpi.sent("POST", "Assistance/Change/2/TeamMember")[0].body == expected
    assert glpi.sent("DELETE", "Assistance/Change/2/TeamMember")[0].body == expected
    assert result.data["removed"] is True


async def test_delete_itil_force_purges(config, client_for, glpi):
    glpi.delete("Assistance/Ticket/5", status=200)
    async with client_for(config) as client:
        await client.call_tool("glpi_delete_itil", {"item_id": 5, "force": True})
    assert glpi.sent("DELETE", "Assistance/Ticket/5")[0].param("force") == "true"


async def test_api_error_is_a_tool_error_with_hint(config, client_for, glpi):
    glpi.get(
        "Assistance/Ticket/99",
        status=404,
        payload={"status": "ERROR_ITEM_NOT_FOUND", "title": "Not found"},
    )
    async with client_for(config) as client:
        with pytest.raises(ToolError, match=r"HTTP 404.*ERROR_ITEM_NOT_FOUND.*exist"):
            await client.call_tool("glpi_get_itil", {"item_id": 99})


async def test_bad_password_grant_explains_the_settings(config, client_for, glpi):
    glpi.routes[("POST", "/api.php/token")] = [(401, {"error": "invalid_client"}, {})]
    async with client_for(config) as client:
        with pytest.raises(ToolError, match="GLPI_CLIENT_ID"):
            await client.call_tool("glpi_whoami", {})


async def test_list_endpoints_and_describe_schema(config, client_for, glpi):
    glpi.get("doc.json", payload=OPENAPI)
    async with client_for(config) as client:
        endpoints = await client.call_tool("glpi_list_endpoints", {"prefix": "assets"})
        names = await client.call_tool("glpi_describe_schema", {})
        ticket = await client.call_tool("glpi_describe_schema", {"name": "ticket"})
        with pytest.raises(ToolError, match="No schema named 'Tick'"):
            await client.call_tool("glpi_describe_schema", {"name": "Tick"})
    paths = [e["path"] for e in endpoints.data["endpoints"]]
    assert "Assets/Computer" in paths
    assert "Assistance/Ticket" not in paths
    assert endpoints.data["endpoints"][paths.index("Assets/Computer")] == {
        "path": "Assets/Computer",
        "methods": ["GET", "POST"],
        "summary": "List computers",
    }
    assert names.data["schemas"] == ["Computer", "Ticket"]
    assert ticket.data["name"] == "Ticket"
    # Internal x-* keys are dropped.
    assert "x-itemtype" not in ticket.data["schema"]
    assert ticket.data["schema"]["properties"]["status"]["properties"]["id"] == {
        "type": "integer",
        "enum": [1, 2],
    }


async def test_list_asset_types(config, client_for, glpi):
    glpi.get("doc.json", payload=OPENAPI)
    async with client_for(config) as client:
        result = await client.call_tool("glpi_list_asset_types", {})
    assert result.data["asset_types"] == ["Computer", "Custom/Badge", "Monitor"]


async def test_search_assets_defaults_to_global(config, client_for, glpi):
    glpi.get("Assets/Global", payload=[{"id": 1, "_itemtype": "Computer"}])
    async with client_for(config) as client:
        await client.call_tool("glpi_search_assets", {"serial": "ABC 1"})
    assert glpi.sent("GET", "Assets/Global")[0].param("filter") == 'serial=="ABC 1"'


async def test_get_asset_with_infocom(config, client_for, glpi):
    glpi.get("Assets/Computer/3", payload={"id": 3, "name": "pc"})
    glpi.get("Assets/Computer/3/Infocom", payload={"warranty_duration": 36})
    async with client_for(config) as client:
        result = await client.call_tool(
            "glpi_get_asset",
            {"asset_type": "Computer", "asset_id": 3, "include_infocom": True},
        )
    assert result.data == {
        "id": 3,
        "name": "pc",
        "infocom": {"warranty_duration": 36},
    }


@pytest.mark.parametrize(
    "path", ["../token", "token", "Assets/Computer?x=1", "Assets/../../etc", ""]
)
async def test_generic_tools_reject_unsafe_paths(config, client_for, glpi, path):
    async with client_for(config) as client:
        with pytest.raises(ToolError, match=r"path|endpoint"):
            await client.call_tool("glpi_list_items", {"path": path})
    assert glpi.api_requests() == []


async def test_list_items_projects_fields(config, client_for, glpi):
    glpi.get(
        "Management/Contract",
        payload=[{"id": 1, "name": "c", "comment": "x"}, {"id": 2, "name": "d"}],
        headers={"Content-Range": "0-1/5"},
    )
    async with client_for(config) as client:
        result = await client.call_tool(
            "glpi_list_items",
            {"path": "Management/Contract", "fields": ["id"], "limit": 2},
        )
    assert result.data["results"] == [{"id": 1}, {"id": 2}]
    assert result.data["next_start"] == 2


async def test_get_user_by_login_with_a_dot(config, client_for, glpi):
    glpi.get("Administration/User", payload=[{"id": 8, "username": "j.doe"}])
    async with client_for(config) as client:
        result = await client.call_tool("glpi_get_user", {"username": "j.doe"})
    assert result.data["id"] == 8
    assert glpi.sent("GET", "Administration/User")[0].param("filter") == (
        "username==j.doe"
    )


async def test_get_user_refuses_an_ambiguous_login(config, client_for, glpi):
    glpi.get(
        "Administration/User",
        payload=[{"id": 8, "username": "j.doe"}, {"id": 9, "username": "j.doe"}],
    )
    async with client_for(config) as client:
        with pytest.raises(ToolError, match=r"IDs \[8, 9\]"):
            await client.call_tool("glpi_get_user", {"username": "j.doe"})


async def test_get_user_by_plain_login_uses_the_route(config, client_for, glpi):
    glpi.get("Administration/User/username/jdoe", payload={"id": 8})
    async with client_for(config) as client:
        result = await client.call_tool("glpi_get_user", {"username": "jdoe"})
    assert result.data == {"id": 8}


async def test_kb_create_files_under_categories_on_2_3(config, client_for, glpi):
    glpi.post("Knowledgebase/Article", status=201, payload={"id": 4})
    async with client_for(config) as client:
        await client.call_tool(
            "glpi_create_kb_article",
            {"name": "VPN", "content": "Steps", "filed_under": [2]},
        )
    assert glpi.sent("POST", "Knowledgebase/Article")[0].body["categories"] == [
        {"id": 2}
    ]


async def test_kb_create_files_under_parents_on_3_0(config):
    glpi = FakeGlpi(API_VERSIONS_12, version="3.0")
    glpi.post("Knowledgebase/Article", status=201, payload={"id": 4})
    glpi.get("Knowledgebase/Article", payload=[{"id": 1, "name": "Root"}])
    async with Client(build_server(config, glpi.transport)) as client:
        await client.call_tool(
            "glpi_create_kb_article",
            {"name": "VPN", "content": "Steps", "filed_under": [2]},
        )
        categories = await client.call_tool("glpi_list_kb_categories", {})
    assert glpi.sent("POST", "Knowledgebase/Article")[0].body["parents"] == [{"id": 2}]
    assert "note" in categories.data
    assert glpi.sent("GET", "Knowledgebase/Article")[0].param("filter") == (
        "parents=empty="
    )


async def test_download_text_and_binary(config, client_for, glpi):
    glpi.get(
        "Management/Document/1/Download",
        content=b"log line",
        headers={"Content-Type": "text/plain; charset=utf-8"},
    )
    glpi.get(
        "Management/Document/2/Download",
        content=b"\x89PNG",
        headers={"Content-Type": "image/png"},
    )
    async with client_for(config) as client:
        text = await client.call_tool("glpi_download_document", {"document_id": 1})
        binary = await client.call_tool("glpi_download_document", {"document_id": 2})
        with pytest.raises(ToolError, match="more than max_bytes"):
            await client.call_tool(
                "glpi_download_document", {"document_id": 1, "max_bytes": 2}
            )
    assert text.data["text"] == "log line"
    assert base64.b64decode(binary.data["base64"]) == b"\x89PNG"


async def test_graphql_query(config, client_for, glpi):
    glpi.post("GraphQL", payload={"data": {"Ticket": []}})
    async with client_for(config) as client:
        result = await client.call_tool(
            "glpi_graphql_query", {"query": "{ Ticket { id } }"}
        )
    assert result.data == {"data": {"Ticket": []}}
    assert glpi.sent("POST", "GraphQL")[0].body == {"query": "{ Ticket { id } }"}


def test_plain_text_fields_keep_angle_brackets():
    from glpi_mcp.tools.common import html_to_text

    assert html_to_text("Owner: John <john@example.com>") == (
        "Owner: John <john@example.com>"
    )
    assert html_to_text("R&amp;D") == "R&D"
    assert html_to_text("<table><tr><td>Name</td><td>John</td></tr></table>") == (
        "Name\tJohn"
    )


def test_html_to_text_drops_style_and_script_and_keeps_br_single():
    from glpi_mcp.tools.common import html_to_text

    mail = (
        "<html><head><style>p { color: red; }</style></head>"
        "<body><p>Hello</p><script>alert(1)</script></body></html>"
    )
    assert html_to_text(mail) == "Hello"
    assert html_to_text("<p>a<br>b<br/>c</p>") == "a\nb\nc"


# ------------------------------------------------------------ GLPI errors

# (tool, arguments, request GLPI refuses, message prefix). FakeGlpi answers
# every unmocked request with HTTP 418, so each call fails on that request.
ERROR_CALLS = [
    ("glpi_whoami", {}, "GET session", "Error reading the GLPI session"),
    (
        "glpi_get_entity_tree",
        {},
        "GET Session/EntityTree",
        "Error reading the entity tree",
    ),
    ("glpi_list_endpoints", {}, "GET doc.json", "Error listing endpoints"),
    ("glpi_describe_schema", {}, "GET doc.json", "Error describing schema"),
    (
        "glpi_list_items",
        {"path": "Management/Contract"},
        "GET Management/Contract",
        "Error listing items",
    ),
    (
        "glpi_get_item",
        {"path": "Management/Contract", "item_id": 2},
        "GET Management/Contract/2",
        "Error getting item",
    ),
    (
        "glpi_create_item",
        {"path": "Management/Contract", "payload": {"name": "c"}},
        "POST Management/Contract",
        "Error creating item",
    ),
    (
        "glpi_update_item",
        {"path": "Management/Contract", "item_id": 2, "payload": {"name": "c"}},
        "PATCH Management/Contract/2",
        "Error updating item",
    ),
    (
        "glpi_delete_item",
        {"path": "Management/Contract", "item_id": 2},
        "DELETE Management/Contract/2",
        "Error deleting item",
    ),
    (
        "glpi_graphql_query",
        {"query": "{ Ticket { id } }"},
        "POST GraphQL",
        "Error running GraphQL query",
    ),
    (
        "glpi_search_itil",
        {"itil_type": "Problem"},
        "GET Assistance/Problem",
        "Error searching problems",
    ),
    (
        "glpi_get_itil",
        {"item_id": 3, "itil_type": "Change"},
        "GET Assistance/Change/3",
        "Error getting change 3",
    ),
    (
        "glpi_get_itil_timeline",
        {"item_id": 3},
        "GET Assistance/Ticket/3/Timeline",
        "Error getting the timeline of Ticket 3",
    ),
    (
        "glpi_create_itil",
        {"name": "t", "content": "c", "itil_type": "Problem"},
        "POST Assistance/Problem",
        "Error creating problem",
    ),
    (
        "glpi_update_itil",
        {"item_id": 3, "changes": {"status": 2}},
        "PATCH Assistance/Ticket/3",
        "Error updating ticket 3",
    ),
    (
        "glpi_delete_itil",
        {"item_id": 3},
        "DELETE Assistance/Ticket/3",
        "Error deleting ticket 3",
    ),
    (
        "glpi_add_itil_followup",
        {"item_id": 3, "content": "c"},
        "POST Assistance/Ticket/3/Timeline/Followup",
        "Error adding a followup to Ticket 3",
    ),
    (
        "glpi_add_itil_task",
        {"item_id": 3, "content": "c"},
        "POST Assistance/Ticket/3/Timeline/Task",
        "Error adding a task to Ticket 3",
    ),
    (
        "glpi_add_itil_solution",
        {"item_id": 3, "content": "c"},
        "POST Assistance/Ticket/3/Timeline/Solution",
        "Error adding a solution to Ticket 3",
    ),
    (
        "glpi_request_itil_validation",
        {"item_id": 3, "approver_id": 2},
        "POST Assistance/Ticket/3/Timeline/Validation",
        "Error requesting approval of Ticket 3",
    ),
    (
        "glpi_list_itil_team",
        {"item_id": 3},
        "GET Assistance/Ticket/3/TeamMember",
        "Error listing the team of Ticket 3",
    ),
    (
        "glpi_add_itil_team_member",
        {"item_id": 3, "member_type": "User", "member_id": 2, "role": "observer"},
        "POST Assistance/Ticket/3/TeamMember",
        "Error adding a team member to Ticket 3",
    ),
    (
        "glpi_remove_itil_team_member",
        {"item_id": 3, "member_type": "User", "member_id": 2, "role": "observer"},
        "DELETE Assistance/Ticket/3/TeamMember",
        "Error removing a team member from Ticket 3",
    ),
    ("glpi_list_asset_types", {}, "GET doc.json", "Error listing asset types"),
    ("glpi_search_assets", {}, "GET Assets/Global", "Error searching assets"),
    (
        "glpi_get_asset",
        {"asset_type": "Printer", "asset_id": 4},
        "GET Assets/Printer/4",
        "Error getting Printer 4",
    ),
    (
        "glpi_create_asset",
        {"asset_type": "Printer", "payload": {"name": "p"}},
        "POST Assets/Printer",
        "Error creating Printer",
    ),
    (
        "glpi_update_asset",
        {"asset_type": "Printer", "asset_id": 4, "changes": {"name": "p"}},
        "PATCH Assets/Printer/4",
        "Error updating Printer 4",
    ),
    (
        "glpi_delete_asset",
        {"asset_type": "Printer", "asset_id": 4},
        "DELETE Assets/Printer/4",
        "Error deleting Printer 4",
    ),
    (
        "glpi_search_users",
        {},
        "GET Administration/User",
        "Error searching users",
    ),
    (
        "glpi_get_user",
        {"user_id": 8},
        "GET Administration/User/8",
        "Error getting user",
    ),
    (
        "glpi_search_groups",
        {},
        "GET Administration/Group",
        "Error searching groups",
    ),
    (
        "glpi_get_group",
        {"group_id": 5},
        "GET Administration/Group/5",
        "Error getting group 5",
    ),
    (
        "glpi_search_entities",
        {},
        "GET Administration/Entity",
        "Error searching entities",
    ),
    (
        "glpi_get_entity",
        {"entity_id": 0},
        "GET Administration/Entity/0",
        "Error getting entity 0",
    ),
    (
        "glpi_list_profiles",
        {},
        "GET Administration/Profile",
        "Error listing profiles",
    ),
    (
        "glpi_search_kb_articles",
        {},
        "GET Knowledgebase/Article",
        "Error searching the knowledge base",
    ),
    (
        "glpi_get_kb_article",
        {"article_id": 6},
        "GET Knowledgebase/Article/6",
        "Error getting KB article 6",
    ),
    (
        "glpi_create_kb_article",
        {"name": "n", "content": "c"},
        "POST Knowledgebase/Article",
        "Error creating KB article",
    ),
    (
        "glpi_update_kb_article",
        {"article_id": 6, "changes": {"name": "n"}},
        "PATCH Knowledgebase/Article/6",
        "Error updating KB article 6",
    ),
    (
        "glpi_list_kb_categories",
        {},
        "GET Knowledgebase/Category",
        "Error listing KB categories",
    ),
    (
        "glpi_search_documents",
        {},
        "GET Management/Document",
        "Error searching documents",
    ),
    (
        "glpi_get_document",
        {"document_id": 7},
        "GET Management/Document/7",
        "Error getting document 7",
    ),
    (
        "glpi_download_document",
        {"document_id": 7},
        "GET Management/Document/7/Download",
        "Error downloading document 7",
    ),
]


@pytest.mark.parametrize(
    ("tool", "args", "request_line", "prefix"),
    ERROR_CALLS,
    ids=[c[0] for c in ERROR_CALLS],
)
async def test_glpi_error_becomes_a_tool_error(
    config, client_for, tool, args, request_line, prefix
):
    async with client_for(config) as client:
        with pytest.raises(ToolError) as info:
            await client.call_tool(tool, args)
    assert str(info.value) == (
        f'{prefix}: HTTP 418 from GLPI ({request_line}): {{"detail": "not mocked"}}'
    )


def test_every_tool_has_an_error_case():
    # glpi_server_info only reads the version list; see the next test.
    assert {c[0] for c in ERROR_CALLS} | {"glpi_server_info"} == (
        READ_TOOLS | WRITE_TOOLS
    )


async def test_server_info_reports_a_glpi_without_the_api(config, client_for, glpi):
    glpi.routes[("GET", "/api.php/")] = [(404, None, {})]
    async with client_for(config) as client:
        with pytest.raises(
            ToolError, match=r"^Error reading GLPI server info: .*11\.0\.6"
        ):
            await client.call_tool("glpi_server_info", {})


# ------------------------------------------------------------ session


def test_auth_mode_names_each_mode(config):
    from glpi_mcp.tools.session import auth_mode

    assert auth_mode(config) == "password-grant (as glpi)"
    assert auth_mode(config.model_copy(update={"token_passthrough": True})) == (
        "token-passthrough (each caller's own GLPI token)"
    )
    assert auth_mode(config.model_copy(update={"oauth_login": True})) == (
        "glpi-login (each user's own GLPI account)"
    )


async def test_whoami_passes_an_unexpected_session_through(config, client_for, glpi):
    glpi.get("session", payload=["not", "an", "object"])
    glpi.get("Administration/User/Me", payload={"id": 2})
    async with client_for(config) as client:
        result = await client.call_tool("glpi_whoami", {})
    assert result.data == {"session": ["not", "an", "object"], "user": {"id": 2}}


async def test_whoami_reports_other_user_errors(config, client_for, glpi):
    glpi.get("session", payload={"user_id": 2})
    glpi.get("Administration/User/Me", status=404, payload={"status": "ERROR"})
    async with client_for(config) as client:
        with pytest.raises(ToolError, match=r"HTTP 404 .*GET Administration/User/Me"):
            await client.call_tool("glpi_whoami", {})


async def test_get_entity_tree(config, client_for, glpi):
    tree = {"id": 0, "name": "Root", "children": [{"id": 1, "name": "Paris"}]}
    glpi.get("Session/EntityTree", payload=tree)
    async with client_for(config) as client:
        result = await client.call_tool("glpi_get_entity_tree", {})
    assert result.data == {"entity_tree": tree}


# ------------------------------------------------------------ administration


async def test_search_users_matches_names_of_active_users(config, client_for, glpi):
    glpi.get(
        "Administration/User",
        payload=[{"id": 8, "username": "jdoe", "password_last_update": "x"}],
    )
    async with client_for(config) as client:
        result = await client.call_tool(
            "glpi_search_users", {"text": "do*e", "filters": {"entity.id": 1}}
        )
        await client.call_tool("glpi_search_users", {"active_only": False})
    first, second = glpi.sent("GET", "Administration/User")
    assert first.param("filter") == (
        "(username=ilike=*doe*,firstname=ilike=*doe*,realname=ilike=*doe*);"
        "is_active==1;entity.id==1"
    )
    # Only the summary properties are returned.
    assert result.data["results"] == [{"id": 8, "username": "jdoe"}]
    assert second.param("filter") is None


async def test_get_user_by_id(config, client_for, glpi):
    glpi.get("Administration/User/8", payload={"id": 8, "comment": "<p>VIP</p>"})
    async with client_for(config) as client:
        result = await client.call_tool("glpi_get_user", {"user_id": 8})
    assert result.data == {"id": 8, "comment": "VIP"}


@pytest.mark.parametrize("args", [{}, {"user_id": 8, "username": "jdoe"}])
async def test_get_user_needs_exactly_one_key(config, client_for, glpi, args):
    async with client_for(config) as client:
        with pytest.raises(ToolError, match="exactly one of 'user_id' and 'username'"):
            await client.call_tool("glpi_get_user", args)
    assert glpi.api_requests() == []


async def test_get_user_reports_an_unknown_login(config, client_for, glpi):
    glpi.get("Administration/User", payload=[])
    async with client_for(config) as client:
        with pytest.raises(ToolError, match=r"No user with login 'j\.doe'"):
            await client.call_tool("glpi_get_user", {"username": "j.doe"})


async def test_search_groups_and_entities_match_the_name(config, client_for, glpi):
    glpi.get("Administration/Group", payload=[{"id": 5, "name": "Helpdesk"}])
    glpi.get("Administration/Entity", payload=[{"id": 1, "name": "Paris"}])
    async with client_for(config) as client:
        groups = await client.call_tool(
            "glpi_search_groups", {"text": "help", "sort": "name:asc"}
        )
        entities = await client.call_tool(
            "glpi_search_entities", {"text": "par", "start": 10}
        )
    [group_request] = glpi.sent("GET", "Administration/Group")
    assert group_request.param("filter") == (
        "name=ilike=*help*,completename=ilike=*help*"
    )
    assert group_request.param("sort") == "name:asc"
    assert groups.data["results"] == [{"id": 5, "name": "Helpdesk"}]
    [entity_request] = glpi.sent("GET", "Administration/Entity")
    assert entity_request.param("filter") == (
        "name=ilike=*par*,completename=ilike=*par*"
    )
    assert entity_request.param("start") == "10"
    assert entity_request.param("limit") == "50"
    assert entities.data["start"] == 10


async def test_get_group_entity_and_list_profiles(config, client_for, glpi):
    glpi.get("Administration/Group/5", payload={"id": 5, "comment": "<b>L1</b>"})
    glpi.get("Administration/Entity/0", payload={"id": 0, "name": "Root"})
    glpi.get("Administration/Profile", payload=[{"id": 4, "name": "Super-Admin"}])
    async with client_for(config) as client:
        group = await client.call_tool("glpi_get_group", {"group_id": 5})
        entity = await client.call_tool("glpi_get_entity", {"entity_id": 0})
        profiles = await client.call_tool("glpi_list_profiles", {"limit": 5})
    assert group.data == {"id": 5, "comment": "L1"}
    assert entity.data == {"id": 0, "name": "Root"}
    assert profiles.data["results"] == [{"id": 4, "name": "Super-Admin"}]
    [request] = glpi.sent("GET", "Administration/Profile")
    assert request.param("filter") is None
    assert request.param("limit") == "5"


# ------------------------------------------------------------ assets


async def test_search_assets_of_one_type(config, client_for, glpi):
    glpi.get("Assets/Printer", payload=[{"id": 1, "name": "hp"}])
    async with client_for(config) as client:
        await client.call_tool(
            "glpi_search_assets",
            {
                "asset_type": "Printer",
                "name": "h*p",
                "otherserial": "INV-1",
                "entity_id": 0,
                "filter": "states.id==2",
            },
        )
    [request] = glpi.sent("GET", "Assets/Printer")
    assert request.param("filter") == (
        "name=ilike=*hp*;otherserial==INV-1;entity.id==0;states.id==2"
    )


async def test_get_asset_reports_infocom_errors_other_than_404(
    config, client_for, glpi
):
    glpi.get("Assets/Computer/3", payload={"id": 3})
    glpi.get("Assets/Computer/3/Infocom", status=403, payload={"status": "ERROR"})
    async with client_for(config) as client:
        with pytest.raises(
            ToolError, match=r"^Error getting Computer 3: HTTP 403 .*Infocom"
        ):
            await client.call_tool(
                "glpi_get_asset",
                {"asset_type": "Computer", "asset_id": 3, "include_infocom": True},
            )


async def test_asset_writes(config, client_for, glpi):
    glpi.post("Assets/Monitor", status=201, payload={"id": 9})
    glpi.patch("Assets/Monitor/9", payload={"id": 9})
    glpi.delete("Assets/Monitor/9", status=204)
    async with client_for(config) as client:
        created = await client.call_tool(
            "glpi_create_asset", {"asset_type": "Monitor", "payload": {"name": "m"}}
        )
        await client.call_tool(
            "glpi_update_asset",
            {"asset_type": "Monitor", "asset_id": 9, "changes": {"location": 4}},
        )
        deleted = await client.call_tool(
            "glpi_delete_asset", {"asset_type": "Monitor", "asset_id": 9}
        )
    assert created.data == {"created": True, "endpoint": "Assets/Monitor", "id": 9}
    assert glpi.sent("POST", "Assets/Monitor")[0].body == {"name": "m"}
    assert glpi.sent("PATCH", "Assets/Monitor/9")[0].body == {"location": 4}
    assert glpi.sent("DELETE", "Assets/Monitor/9")[0].param("force") is None
    assert deleted.data == {
        "deleted": True,
        "purged": False,
        "endpoint": "Assets/Monitor/9",
    }


# ------------------------------------------------------------ documents


async def test_search_and_get_documents(config, client_for, glpi):
    glpi.get("Management/Document", payload=[{"id": 7, "name": "invoice"}])
    glpi.get(
        "Management/Document/7", payload={"id": 7, "comment": "&lt;p&gt;Q1&lt;/p&gt;"}
    )
    async with client_for(config) as client:
        found = await client.call_tool(
            "glpi_search_documents", {"text": "inv", "filters": {"is_deleted": False}}
        )
        document = await client.call_tool("glpi_get_document", {"document_id": 7})
    [request] = glpi.sent("GET", "Management/Document")
    assert request.param("filter") == (
        "(name=ilike=*inv*,filename=ilike=*inv*);is_deleted==0"
    )
    assert found.data["results"] == [{"id": 7, "name": "invoice"}]
    # Entity-encoded HTML, as older GLPI versions stored it, is decoded too.
    assert document.data == {"id": 7, "comment": "Q1"}


async def test_download_text_that_is_not_utf8_is_base64(config, client_for, glpi):
    glpi.get(
        "Management/Document/1/Download",
        content=b"na\xefve",
        headers={"Content-Type": "text/plain"},
    )
    async with client_for(config) as client:
        result = await client.call_tool("glpi_download_document", {"document_id": 1})
    assert "text" not in result.data
    assert base64.b64decode(result.data["base64"]) == b"na\xefve"
    assert result.data["size"] == 5


# ------------------------------------------------------------ generic


async def test_describe_schema_summarizes_nested_properties(config, client_for, glpi):
    doc = {
        "components": {
            "schemas": {
                "Computer": {
                    "type": "object",
                    "description": "  A computer.  ",
                    "x-table": "glpi_computers",
                    "properties": {
                        "user": {"$ref": "#/components/schemas/User"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "odd": True,
                        "location": {
                            "type": "object",
                            "properties": {
                                "address": {
                                    "type": "object",
                                    "properties": {"street": {}, "city": {}},
                                }
                            },
                        },
                    },
                }
            }
        }
    }
    glpi.get("doc.json", payload=doc)
    async with client_for(config) as client:
        result = await client.call_tool("glpi_describe_schema", {"name": "computer"})
        with pytest.raises(
            ToolError, match=r"No schema named 'puter'\. Similar: Computer"
        ):
            await client.call_tool("glpi_describe_schema", {"name": "puter"})
    assert result.data == {
        "name": "Computer",
        "schema": {
            "type": "object",
            "description": "A computer.",
            "properties": {
                "user": {"$ref": "User"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "odd": True,
                "location": {
                    "type": "object",
                    # Deeper levels only list property names.
                    "properties": {
                        "address": {"type": "object", "properties": ["city", "street"]}
                    },
                },
            },
        },
    }


async def test_list_endpoints_skips_paths_without_operations(config, client_for, glpi):
    doc = {
        "paths": {
            "/Assets/Computer": {"parameters": [], "get": {"summary": " " + "x" * 200}},
            "/Assets/Monitor": {"parameters": []},
            "/Assets/Broken": "not an object",
        }
    }
    glpi.get("doc.json", payload=doc)
    async with client_for(config) as client:
        result = await client.call_tool("glpi_list_endpoints", {})
    assert result.data == {
        "count": 1,
        "endpoints": [
            {"path": "Assets/Computer", "methods": ["GET"], "summary": "x" * 160}
        ],
    }


async def test_get_item_by_path_and_id(config, client_for, glpi):
    glpi.get("Management/Contract/2", payload={"id": 2, "comment": "<p>Renew</p>"})
    glpi.get("Assistance/Ticket/5/TeamMember", payload=[{"id": 2, "type": "User"}])
    async with client_for(config) as client:
        text = await client.call_tool(
            "glpi_get_item", {"path": "Management/Contract", "item_id": 2}
        )
        raw = await client.call_tool(
            "glpi_get_item",
            {"path": "Management/Contract", "item_id": 2, "raw_html": True},
        )
        team = await client.call_tool(
            "glpi_get_item", {"path": "Assistance/Ticket/5/TeamMember"}
        )
    assert text.data == {"id": 2, "comment": "Renew"}
    assert raw.data == {"id": 2, "comment": "<p>Renew</p>"}
    # A list answer has no structured output; it arrives as JSON text.
    assert json.loads(team.content[0].text) == [{"id": 2, "type": "User"}]


async def test_list_items_passes_non_object_rows_through(config, client_for, glpi):
    glpi.get("Dropdowns/Location", payload=[3, {"id": 4, "name": "HQ"}])
    async with client_for(config) as client:
        result = await client.call_tool(
            "glpi_list_items", {"path": "Dropdowns/Location", "fields": ["id"]}
        )
    assert result.data["results"] == [3, {"id": 4}]


# ------------------------------------------------------------ ITIL


async def test_search_itil_filters_by_status_priority_category_and_dates(
    config, client_for, glpi
):
    glpi.get("Assistance/Change", payload=[])
    async with client_for(config) as client:
        await client.call_tool(
            "glpi_search_itil",
            {
                "itil_type": "Change",
                "status": [1, 9],
                "priority": [5, 6],
                "category_id": 3,
                "opened_after": "2026-01-01",
                "opened_before": "2026-02-01",
            },
        )
    [request] = glpi.sent("GET", "Assistance/Change")
    assert request.param("filter") == (
        "status.id=in=(1,9);priority=in=(5,6);category.id==3;"
        "date=ge=2026-01-01;date=le=2026-02-01"
    )


async def test_create_itil_ticket_type_only_for_tickets(config, client_for, glpi):
    async with client_for(config) as client:
        with pytest.raises(ToolError, match=r"^Error creating change: .*tickets only"):
            await client.call_tool(
                "glpi_create_itil",
                {"name": "t", "content": "c", "itil_type": "Change", "ticket_type": 1},
            )
    assert glpi.api_requests() == []


async def test_update_itil_needs_a_change(config, client_for, glpi):
    async with client_for(config) as client:
        with pytest.raises(ToolError, match="at least one property"):
            await client.call_tool("glpi_update_itil", {"item_id": 1, "changes": {}})
    assert glpi.api_requests() == []


@pytest.mark.parametrize(
    ("role", "path"),
    [
        (None, "Assistance/Problem/4/TeamMember"),
        ("assigned", "Assistance/Problem/4/TeamMember/assigned"),
    ],
)
async def test_list_itil_team(config, client_for, glpi, role, path):
    members = [{"type": "User", "id": 2, "role": "assigned"}]
    glpi.get(path, payload=members)
    args = {"item_id": 4, "itil_type": "Problem"}
    if role:
        args["role"] = role
    async with client_for(config) as client:
        result = await client.call_tool("glpi_list_itil_team", args)
    assert result.data == {"team": members}
    assert len(glpi.sent("GET", path)) == 1


# ------------------------------------------------------------ knowledge base


async def test_search_kb_articles(config, client_for, glpi):
    glpi.get(
        "Knowledgebase/Article",
        payload=[{"id": 1, "name": "VPN", "answer": "<p>long</p>", "is_faq": True}],
    )
    async with client_for(config) as client:
        result = await client.call_tool(
            "glpi_search_kb_articles",
            {"text": "vpn", "faq_only": True, "filter": "date_mod=ge=2026-01-01"},
        )
    [request] = glpi.sent("GET", "Knowledgebase/Article")
    assert request.param("filter") == (
        "(name=ilike=*vpn*,content=ilike=*vpn*);is_faq==1;date_mod=ge=2026-01-01"
    )
    assert result.data["results"] == [{"id": 1, "name": "VPN", "is_faq": True}]


async def test_get_kb_article_as_text_or_html(config, client_for, glpi):
    glpi.get("Knowledgebase/Article/6", payload={"id": 6, "answer": "<p>Steps</p>"})
    async with client_for(config) as client:
        text = await client.call_tool("glpi_get_kb_article", {"article_id": 6})
        raw = await client.call_tool(
            "glpi_get_kb_article", {"article_id": 6, "raw_html": True}
        )
    assert text.data == {"id": 6, "answer": "Steps"}
    assert raw.data == {"id": 6, "answer": "<p>Steps</p>"}


async def test_kb_create_in_an_entity(config, client_for, glpi):
    glpi.post("Knowledgebase/Article", status=201, payload={"id": 4})
    async with client_for(config) as client:
        await client.call_tool(
            "glpi_create_kb_article",
            {"name": "VPN", "content": "Steps", "is_faq": True, "entity_id": 0},
        )
    assert glpi.sent("POST", "Knowledgebase/Article")[0].body == {
        "name": "VPN",
        "content": "Steps",
        "is_faq": True,
        "entity": 0,
    }


async def test_list_kb_categories_on_2_3(config, client_for, glpi):
    glpi.get(
        "Knowledgebase/Category",
        payload=[{"id": 2, "name": "Network", "comment": "<p>LAN</p>"}],
    )
    async with client_for(config) as client:
        result = await client.call_tool("glpi_list_kb_categories", {"limit": 10})
    [request] = glpi.sent("GET", "Knowledgebase/Category")
    assert request.param("limit") == "10"
    assert result.data["endpoint"] == "Knowledgebase/Category"
    assert result.data["results"] == [{"id": 2, "name": "Network", "comment": "LAN"}]
    assert "note" not in result.data


@pytest.mark.parametrize("value", [None, 5, "Plain text, no markup", ""])
def test_html_to_text_passes_values_without_markup_through(value):
    assert html_to_text(value) == value
