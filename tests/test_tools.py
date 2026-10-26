import base64

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
