"""MCP tool wiring: client_tag required, no Maps/Apify tools."""

from __future__ import annotations

from mcp_server.server import mcp


def test_tool_names() -> None:
    import asyncio

    async def _names() -> list[str]:
        tools = await mcp.list_tools()
        return sorted(t.name for t in tools)

    names = asyncio.run(_names())
    assert "enrich_waterfall" in names
    assert "health" in names
    assert "get_job_status" in names
    assert "describe_client" in names
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
