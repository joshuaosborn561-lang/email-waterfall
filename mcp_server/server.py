"""Email Waterfall MCP — DM / work-email enrichment only."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from mcp_server.playbook import INSTRUCTIONS, WHEN_TO_USE

ROOT = Path(__file__).resolve().parent.parent

mcp = MCPServer(
    name="email-waterfall",
    title="Email Waterfall",
    description=(
        "Resolve decision-makers and work emails from company domains via a paid "
        "vendor waterfall, then write isolated Basco / Peterson rows to Supabase. "
        "Not a Maps scraper or website crawler."
    ),
    instructions=INSTRUCTIONS,
    version="1.0.0",
)


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


def _ensure_repo_cwd() -> None:
    os.chdir(ROOT)


def _http_mode() -> bool:
    return os.environ.get("MCP_TRANSPORT", "stdio").lower() in (
        "streamable-http",
        "http",
        "sse",
    )


def _reload_settings() -> None:
    from email_waterfall import config as cfg

    cfg.settings = cfg.load_settings()


@mcp.resource(
    "email-waterfall://playbook",
    name="playbook",
    description="When to use this MCP and how enrich_waterfall writes to Supabase.",
    mime_type="text/markdown",
)
def playbook_resource() -> str:
    return INSTRUCTIONS


@mcp.prompt(
    name="when_to_use",
    description="Decide whether the Email Waterfall MCP applies.",
)
def when_to_use_prompt() -> str:
    return WHEN_TO_USE


@mcp.tool(
    annotations=ToolAnnotations(
        title="Health / config check",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def health() -> str:
    """Show which vendor keys and Supabase credentials are configured. Never prints secrets."""
    _ensure_repo_cwd()
    _reload_settings()
    from email_waterfall.clients import CLIENTS
    from email_waterfall.config import settings

    return _json(
        {
            "ok": True,
            "service": "email-waterfall",
            "product": "dm_email_enrichment",
            "not": ["google_maps_scraper", "website_crawler", "apify_contact_scraper"],
            "supabase_configured": settings.supabase_configured,
            "supabase_url": settings.supabase_url or None,
            "vendors": {
                "aiark": bool(settings.ai_ark_api_key),
                "getleads": bool(settings.getleads_api_key),
                "leadmagic": bool(settings.leadmagic_api_key),
                "fullenrich": bool(settings.fullenrich_api_key),
            },
            "clients": {
                tag: {
                    "companies_table": c.companies_table,
                    "contacts_table": c.contacts_table,
                    "owner": c.owner,
                    "titles": list(c.titles),
                }
                for tag, c in CLIENTS.items()
            },
            "max_tier_default": "leadmagic",
            "auth": "none",
        }
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Describe client ICP + write tables",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def describe_client(client_tag: str) -> str:
    """Show isolated tables and ranked DM titles for basco or peterson."""
    from email_waterfall.clients import get_client

    client = get_client(client_tag)
    return _json(
        {
            "client_tag": client.tag,
            "owner": client.owner,
            "icp": client.icp,
            "companies_table": f"public.{client.companies_table}",
            "contacts_table": f"public.{client.contacts_table}",
            "target_titles": list(client.titles),
            "fallback_titles": sorted(client.fallback_titles),
        }
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get background job status",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def get_job_status(job_id: str) -> str:
    """Poll a background enrich_waterfall job. Use after enrich_waterfall returns job_id."""
    from mcp_server.jobs import get_job

    return _json(get_job(job_id).to_public())


@mcp.tool(
    annotations=ToolAnnotations(
        title="List background jobs",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def list_background_jobs(limit: int = 20) -> str:
    """List recent enrich_waterfall jobs on this MCP server."""
    from mcp_server.jobs import list_jobs

    return _json([j.to_public() for j in list_jobs(limit=limit)])


@mcp.tool(
    annotations=ToolAnnotations(
        title="Enrich waterfall → isolated client tables",
        readOnlyHint=False,
        openWorldHint=True,
        destructiveHint=True,
    )
)
def enrich_waterfall(
    rows: Any,
    client_tag: str,
    need: str = "both",
    max_tier: str = "leadmagic",
    target_titles: str = "",
    require_title_match: bool = True,
    background: bool = True,
) -> str:
    """Resolve DMs + work emails via paid vendors; write to public.{client}_* tables.

    `rows` = JSON list of {domain, company_name?, first_name?, last_name?, title?,
    email?, place_id?, city?, state?}. Domain is required.

    client_tag is required: 'basco' | 'peterson'. Never omit it.
    need = 'dm' | 'email' | 'both'.
    max_tier = 'aiark' | 'getleads' | 'leadmagic' | 'fullenrich'
    (default 'leadmagic' — FullEnrich never runs unless explicitly requested).

    target_titles = comma-separated ranked titles. Empty uses the client default.
    require_title_match = drop people whose title is not in the ranked list
    (basco GM / Dealer Principal remains a last-resort fallback).

    Response is counts only. Long runs return job_id — poll get_job_status.
    """
    _ensure_repo_cwd()
    _reload_settings()
    from email_waterfall import waterfall as wf
    from email_waterfall.clients import get_client

    need_norm = (need or "both").strip().lower()
    if need_norm not in ("email", "dm", "both"):
        raise ValueError("need must be 'email', 'dm', or 'both'")
    client = get_client(client_tag)
    max_tier_n = wf.normalize_max_tier(max_tier)

    def _run() -> dict[str, Any]:
        return wf.enrich_waterfall(
            rows,
            client_tag=client.tag,
            need=need_norm,  # type: ignore[arg-type]
            max_tier=max_tier_n,
            target_titles=target_titles,
            require_title_match=bool(require_title_match),
            write_supabase=True,
        )

    rows_chars = len(rows) if isinstance(rows, str) else len(json.dumps(rows, default=str))
    if background and (_http_mode() or rows_chars > 2000):
        from mcp_server.jobs import start_job

        job = start_job(
            "enrich_waterfall",
            _run,
            meta={
                "need": need_norm,
                "max_tier": max_tier_n,
                "client_tag": client.tag,
                "rows_chars": rows_chars,
            },
        )
        return _json(
            {
                "job_id": job.id,
                "status": job.status,
                "message": f"Poll get_job_status with job_id={job.id}.",
                "client_tag": client.tag,
                "companies_table": client.companies_table,
                "contacts_table": client.contacts_table,
            }
        )
    return _json(_run())


def _mount_http_routes() -> None:
    try:
        from starlette.requests import Request
        from starlette.responses import JSONResponse, PlainTextResponse
    except ImportError:
        return

    @mcp.custom_route("/", methods=["GET"])
    async def root_page(_request: Request) -> PlainTextResponse:
        return PlainTextResponse(
            "Email Waterfall MCP\n"
            "Claude / Cursor connector URL: /mcp\n"
            "Health: /health\n"
        )

    @mcp.custom_route("/health", methods=["GET"])
    async def health_live(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "service": "email-waterfall",
                "transport": "streamable-http",
                "mcp_path": "/mcp",
            }
        )


_mount_http_routes()


def main() -> None:
    """stdio for local Cursor/Claude Desktop; streamable-http for Railway."""
    _ensure_repo_cwd()
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))

    if transport in ("streamable-http", "http"):
        kwargs: dict[str, Any] = {
            "transport": "streamable-http",
            "host": host,
            "port": port,
        }
        try:
            from mcp.server.transport_security import TransportSecuritySettings

            kwargs.update(
                {
                    "streamable_http_path": "/mcp",
                    "stateless_http": True,
                    "transport_security": TransportSecuritySettings(
                        enable_dns_rebinding_protection=False
                    ),
                }
            )
        except Exception:
            kwargs["path"] = "/mcp"
        mcp.run(**kwargs)
        return

    if transport == "sse":
        mcp.run(transport="sse", host=host, port=port)
        return

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
