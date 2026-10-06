"""MCP tool wiring: client_tag required, no Maps/Apify tools."""

from __future__ import annotations

from mcp_server.server import mcp


def test_tool_names() -> None:
    import asyncio
    import inspect

    tools = mcp.list_tools()
    if inspect.iscoroutine(tools):
        tools = asyncio.run(tools)
    names = sorted(t.name for t in tools)
    assert "getleads_search" in names
    assert "enrich_waterfall" in names
    assert "health" in names
    assert "get_job_status" in names
    assert "describe_client" in names
    assert "ensure_client" in names
    assert "list_clients" in names
    banned = {
        "scrape_maps",
        "run_leads",
        "plan_leads",
        "enrich_sites",
        "apify_contact_crawl",
        "crawl_team_pages",
        "probe_maps",
    }
    assert banned.isdisjoint(set(names))


def test_health_getleads_is_oauth_snapshot() -> None:
    import json

    from mcp_server.server import health

    data = json.loads(health())
    gl = data["vendors"]["getleads"]
    assert set(gl) >= {"configured", "auth_ok", "reason", "tools"}
    assert gl["configured"] is False
    assert gl["auth_ok"] is False
    assert "veriphone" in data["vendors"]
    assert data["vendors"]["veriphone"] is False


def test_enrich_waterfall_has_source_and_estimate_only() -> None:
    import inspect

    from mcp_server.server import enrich_waterfall

    params = inspect.signature(enrich_waterfall).parameters
    assert "source" in params
    assert "source_table" in params
    assert "where" in params
    assert "estimate_only" in params
    assert "writeback" in params
    assert "verify_only" in params
    assert params["verify_only"].default is False
    assert params["rows"].default is None
    assert params["client_tag"].default is inspect.Parameter.empty
    assert "find_people" in params
    assert "find_email" in params
    assert "find_phone" in params
    assert params["find_phone"].default is None


def _schema_types(prop: dict) -> set[str]:
    if "type" in prop:
        return {prop["type"]}
    types: set[str] = set()
    for item in prop.get("anyOf") or prop.get("oneOf") or []:
        if "type" in item:
            types.add(item["type"])
    return types


def test_enrich_waterfall_schema_types_rows_and_source() -> None:
    """Untyped Any properties make some MCP hosts refuse tools/call with no log."""
    import asyncio
    import inspect

    tools = mcp.list_tools()
    if inspect.iscoroutine(tools):
        tools = asyncio.run(tools)
    schema = next(t.input_schema for t in tools if t.name == "enrich_waterfall")
    props = schema["properties"]
    assert _schema_types(props["rows"]) >= {"array", "string", "null"}
    assert _schema_types(props["source"]) >= {"object", "string", "null"}
    assert _schema_types(props["source_table"]) >= {"string", "object", "null"}
    assert _schema_types(props["where"]) >= {"string", "null"}
    assert _schema_types(props["target_titles"]) >= {"string"}
    assert "client_tag" in schema.get("required", [])


def test_mcp_accepts_table_source_shapes() -> None:
    """Hosts send table-name strings, objects, and null companion fields."""
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "enrich_waterfall")
    validate = tool.fn_metadata.validate_arguments
    validate(
        {
            "client_tag": "peterson",
            "estimate_only": True,
            "source_table": "client_peterson.email_resolution",
            "where": "dl_status is null",
            "source": None,
        }
    )
    validate(
        {
            "client_tag": "peterson",
            "estimate_only": True,
            "source": "client_peterson.email_resolution",
            "source_table": None,
            "where": None,
        }
    )
    validate(
        {
            "client_tag": "peterson",
            "estimate_only": True,
            "source_table": {
                "table": "client_peterson.email_resolution",
                "where": "dl_status is null",
            },
        }
    )
    validate(
        {
            "client_tag": "peterson",
            "estimate_only": True,
            "source": {"table": "client_peterson.email_resolution"},
            "source_table": None,
        }
    )


def test_mcp_enrich_error_includes_exception_message() -> None:
    import pytest
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp_server.server import enrich_waterfall

    with pytest.raises(ToolError, match="need must be"):
        enrich_waterfall(
            client_tag="peterson",
            need="not-a-need",
            estimate_only=True,
            rows=[{"domain": "x.com"}],
        )
