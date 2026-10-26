"""DRY_RUN_MODE: write tools describe the request and never send it."""

import pytest

WRITE_CALLS = [
    (
        "glpi_create_item",
        {"path": "Management/Contract", "payload": {"name": "c"}},
        "POST",
        "Management/Contract",
    ),
    (
        "glpi_update_item",
        {"path": "Assets/Computer", "item_id": 3, "payload": {"name": "pc"}},
        "PATCH",
        "Assets/Computer/3",
    ),
    (
        "glpi_delete_item",
        {"path": "Assets/Computer", "item_id": 3, "force": True},
        "DELETE",
        "Assets/Computer/3",
    ),
    (
        "glpi_create_itil",
        {"name": "t", "content": "c"},
        "POST",
        "Assistance/Ticket",
    ),
    (
        "glpi_update_itil",
        {"item_id": 1, "changes": {"status": 2}},
        "PATCH",
        "Assistance/Ticket/1",
    ),
    ("glpi_delete_itil", {"item_id": 1}, "DELETE", "Assistance/Ticket/1"),
    (
        "glpi_add_itil_followup",
        {"item_id": 1, "content": "c"},
        "POST",
        "Assistance/Ticket/1/Timeline/Followup",
    ),
    (
        "glpi_add_itil_task",
        {"item_id": 1, "content": "c"},
        "POST",
        "Assistance/Ticket/1/Timeline/Task",
    ),
    (
        "glpi_add_itil_solution",
        {"item_id": 1, "content": "c"},
        "POST",
        "Assistance/Ticket/1/Timeline/Solution",
    ),
    (
        "glpi_request_itil_validation",
        {"item_id": 1, "approver_id": 2},
        "POST",
        "Assistance/Ticket/1/Timeline/Validation",
    ),
    (
        "glpi_add_itil_team_member",
        {"item_id": 1, "member_type": "User", "member_id": 2, "role": "assigned"},
        "POST",
        "Assistance/Ticket/1/TeamMember",
    ),
    (
        "glpi_remove_itil_team_member",
        {"item_id": 1, "member_type": "User", "member_id": 2, "role": "assigned"},
        "DELETE",
        "Assistance/Ticket/1/TeamMember",
    ),
    (
        "glpi_create_asset",
        {"asset_type": "Monitor", "payload": {"name": "m"}},
        "POST",
        "Assets/Monitor",
    ),
    (
        "glpi_update_asset",
        {"asset_type": "Monitor", "asset_id": 4, "changes": {"name": "m"}},
        "PATCH",
        "Assets/Monitor/4",
    ),
    (
        "glpi_delete_asset",
        {"asset_type": "Monitor", "asset_id": 4},
        "DELETE",
        "Assets/Monitor/4",
    ),
    (
        "glpi_create_kb_article",
        {"name": "n", "content": "c"},
        "POST",
        "Knowledgebase/Article",
    ),
    (
        "glpi_update_kb_article",
        {"article_id": 5, "changes": {"name": "n"}},
        "PATCH",
        "Knowledgebase/Article/5",
    ),
]


@pytest.mark.parametrize(
    ("tool", "args", "method", "endpoint"),
    WRITE_CALLS,
    ids=[c[0] for c in WRITE_CALLS],
)
async def test_dry_run_sends_nothing(
    config, client_for, glpi, tool, args, method, endpoint
):
    dry = config.model_copy(update={"dry_run_mode": True})
    async with client_for(dry) as client:
        result = await client.call_tool(tool, args)
    assert result.data["dry_run"] is True
    assert result.data["method"] == method
    assert result.data["endpoint"] == endpoint
    assert all(r.method == "GET" for r in glpi.requests)


def test_every_write_tool_is_covered():
    from tests.test_tools import WRITE_TOOLS

    assert {c[0] for c in WRITE_CALLS} == WRITE_TOOLS
