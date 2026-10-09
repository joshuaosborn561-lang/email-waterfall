"""Email Waterfall MCP — DM / work-email enrichment only."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from mcp_server.playbook import INSTRUCTIONS, WHEN_TO_USE

ROOT = Path(__file__).resolve().parent.parent

mcp = MCPServer(
    name="email-waterfall",
    title="Email Waterfall",
    description=(
        "Resolve decision-makers and work emails from company domains via a paid "
        "vendor waterfall, then write isolated per-client rows to Supabase. "
        "Not a Maps scraper or website crawler."
    ),
    instructions=INSTRUCTIONS,
    version="1.6.0",
)


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


def _tool_error(exc: BaseException) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    raise ToolError(f"{type(exc).__name__}: {exc}") from exc


def _table_ref(source: Any, source_table: Any) -> str | None:
    if isinstance(source_table, str) and source_table.strip():
        return source_table.strip()
    if isinstance(source_table, dict):
        name = str(source_table.get("table") or source_table.get("source_table") or "").strip()
        if name:
            return name
    if isinstance(source, str) and source.strip():
        return source.strip()
    if isinstance(source, dict):
        name = str(source.get("table") or source.get("source_table") or "").strip()
        return name or None
    return None


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


PAID_CREDIT_FLOOR = 50


def _credits_remaining(payload: Any) -> float | None:
    if isinstance(payload, (int, float)) and not isinstance(payload, bool):
        return float(payload)
    if not isinstance(payload, dict):
        return None
    for key in (
        "credits_remaining",
        "credits",
        "remaining",
        "balance",
        "total",
        "available",
        "availableCredits",
        "available_credits",
    ):
        val = payload.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val)
        if isinstance(val, dict):
            inner = (
                val.get("remaining")
                or val.get("credits")
                or val.get("total")
                or val.get("available")
            )
            if isinstance(inner, (int, float)) and not isinstance(inner, bool):
                return float(inner)
    return None


def _paid_vendor_balance(name: str, client: Any) -> dict[str, Any]:
    if not getattr(client, "enabled", False):
        return {"configured": False, "credits_remaining": None, "ok": True}
    try:
        payload = client.credits()
    except Exception as exc:
        return {
            "configured": True,
            "credits_remaining": None,
            "ok": True,
            "reason": f"{name}_credits_unavailable: {exc}"[:200],
        }
    remaining = _credits_remaining(payload)
    snap: dict[str, Any] = {
        "configured": True,
        "credits_remaining": remaining,
        "ok": True,
    }
    if remaining is not None and remaining < PAID_CREDIT_FLOOR:
        snap["ok"] = False
        snap["reason"] = (
            f"{name} credits_remaining={remaining:g} (min {PAID_CREDIT_FLOOR})"
        )
    return snap


def _smartlead_credits() -> dict[str, Any]:
    from email_waterfall.vendors.smartlead import SmartleadClient

    client = SmartleadClient(timeout=8)
    if not client.enabled:
        return {"configured": False}
    try:
        client.refresh_credits(force=True)
    except Exception:
        return {"configured": True, "available": None}
    snap = client.credit_snapshot()
    return {"configured": True, **snap}


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
    """Show vendor keys plus live paid-vendor balances. Never prints secrets."""
    _ensure_repo_cwd()
    _reload_settings()
    from email_waterfall.clients import list_registered_clients
    from email_waterfall.config import settings
    from email_waterfall.vendors.ai_ark import AiArkClient
    from email_waterfall.vendors.getleads import GetLeadsClient

    aiark = _paid_vendor_balance("aiark", AiArkClient(timeout=8))
    reasons = [
        snap["reason"]
        for snap in (aiark,)
        if snap.get("configured") and snap.get("ok") is False and snap.get("reason")
    ]
    return _json(
        {
            "ok": not reasons,
            "reason": "; ".join(reasons) if reasons else None,
            "service": "email-waterfall",
            "product": "dm_email_enrichment",
            "not": ["google_maps_scraper", "website_crawler", "apify_contact_scraper"],
            "supabase_configured": settings.supabase_configured,
            "supabase_url": settings.supabase_url or None,
            "vendors": {
                "getleads": GetLeadsClient().health_snapshot(),
                "smartlead": bool(settings.smartlead_api_key),
                "aiark": aiark,
                "prospeo": bool(settings.prospeo_api_key),
                "fullenrich": bool(settings.fullenrich_api_key),
                "veriphone": bool(settings.veriphone_api_key),
            },
            "smartlead_credits": _smartlead_credits(),
            "paid_credit_floor": PAID_CREDIT_FLOOR,
            "clients": {
                c.tag: {
                    "companies_table": c.companies_table,
                    "contacts_table": c.contacts_table,
                    "owner": c.owner,
                    "profile": c.profile,
                    "titles": list(c.titles),
                }
                for c in list_registered_clients()
            },
            "max_tier_default": "prospeo",
            "approve_cost_usd_default": 5.0,  # DEFAULT_APPROVE_COST_USD
            "auth": "none",
            "note": (
                "Any snake_case client_tag works. Call ensure_client or "
                "auto-ensures on enrich_waterfall."
            ),
        }
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Ensure / register a client",
        readOnlyHint=False,
        openWorldHint=False,
        destructiveHint=False,
    )
)
def ensure_client(
    client_tag: str,
    display_name: str = "",
    profile: str = "owner",
    icp: str = "",
    target_titles: str = "",
) -> str:
    """Create public.{tag}_wf_companies / _wf_contacts (or legacy names) and register.

    profile = 'owner' (default ranked owner titles) or 'service' (basco-style).
    target_titles = optional comma-separated ranked titles override.
    Idempotent. enrich_waterfall also auto-ensures.
    """
    _ensure_repo_cwd()
    _reload_settings()
    from email_waterfall.clients import ensure_client as _ensure

    client = _ensure(
        client_tag,
        display_name=display_name,
        profile=profile,
        icp=icp,
        target_titles=target_titles,
        write_supabase=True,
    )
    return _json(
        {
            "ok": True,
            "client_tag": client.tag,
            "display_name": client.display_name,
            "owner": client.owner,
            "profile": client.profile,
            "icp": client.icp,
            "companies_table": f"public.{client.companies_table}",
            "contacts_table": f"public.{client.contacts_table}",
            "target_titles": list(client.titles),
        }
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="List registered clients",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def list_clients() -> str:
    """List registered client_tags and their write tables. No lead payloads."""
    _ensure_repo_cwd()
    _reload_settings()
    from email_waterfall.clients import list_registered_clients

    return _json(
        [
            {
                "client_tag": c.tag,
                "display_name": c.display_name,
                "owner": c.owner,
                "profile": c.profile,
                "companies_table": f"public.{c.companies_table}",
                "contacts_table": f"public.{c.contacts_table}",
                "target_titles": list(c.titles),
                "icp": c.icp,
            }
            for c in list_registered_clients()
        ]
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Describe client ICP + write tables",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def describe_client(client_tag: str) -> str:
    """Show isolated tables and ranked DM titles for a client_tag."""
    from email_waterfall.clients import get_client

    client = get_client(client_tag)
    return _json(
        {
            "client_tag": client.tag,
            "display_name": client.display_name,
            "owner": client.owner,
            "profile": client.profile,
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
        title="Search getleads for new leads (ICP)",
        readOnlyHint=False,
        openWorldHint=True,
        destructiveHint=False,
    )
)
def getleads_search(
    filters: dict[str, Any] | None = None,
    page: int = 0,
    size: int = 100,
    client_tag: str = "",
    write: bool = False,
) -> str:
    """Discover leads via the getleads MCP people/lead search tool.

    Returns counts and the first N compact rows (name, title, company, domain,
    linkedin, state, email yes/no, phone yes/no). Never raw payloads.

    Pass `filters` using the live field names from `filter_schema` (copied from
    getleads tools/list). Typical ICP: geography + industry + seniority +
    headcount + title.

    write=true with client_tag upserts public.{tag}_wf_companies / _wf_contacts
    with source_tool='getleads_search'.
    """
    _ensure_repo_cwd()
    _reload_settings()
    from email_waterfall.clients import ensure_client
    from email_waterfall.supabase_sync import (
        company_row,
        contact_row,
        upsert_contacts,
        upsert_companies,
    )
    from email_waterfall.vendors.getleads import GetLeadsClient, compact_person

    client = GetLeadsClient()
    size_n = max(1, min(int(size or 100), 100))
    page_n = max(0, int(page or 0))
    result = client.search_people(dict(filters or {}), page=page_n, size=size_n)
    people = result.get("people") or []
    compact = [compact_person(p) for p in people]
    preview_n = min(20, len(compact))
    companies_upserted = 0
    contacts_written = 0
    if write:
        tag = (client_tag or "").strip()
        if not tag:
            raise ValueError("client_tag is required when write=true")
        cfg = ensure_client(tag, write_supabase=True)
        companies = []
        contacts = []
        for person, row in zip(people, compact):
            domain = (row.get("domain") or "").strip().lower()
            if not domain:
                continue
            companies.append(
                company_row(
                    client_tag=cfg.tag,
                    domain=domain,
                    company_name=row.get("company") or "",
                    source="getleads_search",
                    address_state=row.get("state") or "",
                    dm_source_tier="getleads",
                    source_tier={"dm": "getleads"},
                    dm_lookup_status="found",
                )
            )
            contacts.append(
                contact_row(
                    client_tag=cfg.tag,
                    domain=domain,
                    first_name=person.first_name,
                    last_name=person.last_name,
                    job_title=person.title,
                    email=person.email,
                    email_status="found" if person.email else "",
                    cellphone=person.phone,
                    linkedin_url=person.linkedin_url,
                    contact_state=row.get("state") or "",
                    source_tool="getleads_search",
                    source_tier="getleads",
                    confidence=0.7 if person.email or person.phone else 0.4,
                )
            )
        if companies:
            companies_upserted = upsert_companies(cfg, companies)
        if contacts:
            contacts_written += upsert_contacts(cfg, contacts)

    return _json(
        {
            "total": result.get("total"),
            "next_page": result.get("next_page"),
            "returned": len(compact),
            "email_present": sum(1 for r in compact if r.get("email")),
            "phone_present": sum(1 for r in compact if r.get("phone")),
            "preview": compact[:preview_n],
            "filter_schema": client.search_filter_schema(),
            "errors": client.errors,
            "write": bool(write),
            "client_tag": (client_tag or "").strip() or None,
            "companies_upserted": companies_upserted,
            "contacts_written": contacts_written,
        }
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Enrich waterfall → isolated client tables",
        readOnlyHint=False,
        openWorldHint=True,
        destructiveHint=True,
    )
)
def enrich_waterfall(
    client_tag: str,
    rows: list[dict[str, Any]] | str | None = None,
    need: str = "both",
    max_tier: str = "prospeo",
    target_titles: str | list[str] | None = None,
    require_title_match: bool = True,
    background: bool = True,
    source: dict[str, Any] | str | None = None,
    source_table: str | dict[str, Any] | None = None,
    where: str | None = None,
    estimate_only: bool = False,
    writeback: bool = True,
    verify_only: bool = False,
    find_people: bool | None = None,
    find_email: bool | None = None,
    find_phone: bool | None = None,
    skip_tiers: str | list[str] | None = None,
    approve_cost_usd: float | None = 5.0,
) -> str:
    """Resolve DMs + work emails + cellphones via paid vendors; write public.{client}_*.

    Pass either `rows` or `source_table`/`source`, never both. Response is
    counts / job_id / cost only — never row payloads.

    Prefer `source_table` + `where` (Maps-scraper style), e.g.
    source_table='client_peterson.email_resolution', where='candidate_email is null'.
    The server pages 500 rows and never returns payloads.

    `rows` = JSON list of {domain?, company_name?, first_name?, last_name?, title?,
    email?, linkedin_url?, phone?, cellphone?, mobile?, place_id?, city?, state?}.
    Domain OR (first_name + last_name + company_name) is required. Name+company
    rows use GetLeads enrich_person_batch, skip Smartlead, then AI Ark →
    Prospeo → FullEnrich. company_name is passed to FullEnrich verbatim.

    `source` = optional richer object {project_id, schema?, table, where?,
    key_column?, map?, limit?, cursor?}. Mutually exclusive with rows.

    Writeback (default true) patches wf_status / wf_email / wf_email_status /
    wf_vendor / wf_updated_at / wf_phone / wf_phone_type on the source table.

    `verify_only` = true checks existing phone numbers with Veriphone and
    writes the number + line type. No finder HTTP. Use this to classify
    numbers already on the queue without paying later paid finders again.

    `estimate_only` = true returns row counts per mode, tiers each mode will
    touch, and a per-vendor credit estimate. Zero spend. Required before paid
    source runs.

    client_tag is required (any snake_case). enrich_waterfall always calls
    ensure_client first — including builtin peterson/basco — so write tables
    exist before the first upsert. Never omit client_tag.

    Optional find_people / find_email / find_phone override `need` when any
    of the three is passed. Phone is off unless find_phone=true or need is
    'both' / 'phone'. If none of the flags is passed, need mapping is unchanged.

    need = 'dm' | 'email' | 'both' | 'phone' | 'people_email'. Gates vendor
    *calls*, not just output. need='people_email' finds people + emails and
    never calls phone endpoints. need='both' includes phone. need='email'
    never hits phone/mobile endpoints (AI Ark mobile-phone-finder, Prospeo
    enrich_mobile, FullEnrich contact.phones, Veriphone).
    need='phone' never hits
    email finders. Vendor-found numbers are sent to Veriphone /v2/verify;
    only phone_valid + phone_type=mobile is written as cellphone. The number and
    Veriphone phone_type are always written back (wf_phone / wf_phone_type,
    contacts.line_type). Existing contact rows upsert on the person key
    (client_tag, domain, first_name_key, last_name_key), not inserted again.
    max_tier caps depth independently.
    max_tier = 'getleads' | 'smartlead' | 'aiark' | 'prospeo' | 'fullenrich'
    (default 'prospeo'). Legacy LeadMagic names (leadmagic / lm / lead_magic)
    are accepted as a no-op warning and map to the old AI Ark ceiling.
    approve_cost_usd (default 5.0) estimates first and refuses if the quote
    exceeds the ceiling; a run that would go over stops with
    status=stopped_at_ceiling. Never read or write dl_status, sg_exclude,
    or skip_* columns — resume with wf_status is null.
    """
    _ensure_repo_cwd()
    _reload_settings()
    from email_waterfall import waterfall as wf
    from email_waterfall.clients import ensure_client

    from email_waterfall.need import resolve_need

    try:
        need_norm, _caps = resolve_need(
            need,
            find_people=find_people,
            find_email=find_email,
            find_phone=find_phone,
        )
    except Exception as exc:
        _tool_error(exc)
    client = ensure_client(client_tag, write_supabase=not estimate_only)
    max_tier_n = wf.normalize_max_tier(max_tier)

    def _run_enrich(
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        return wf.enrich_waterfall(
            rows,
            client_tag=client.tag,
            need=need,  # type: ignore[arg-type]
            max_tier=max_tier,
            target_titles=target_titles,
            require_title_match=bool(require_title_match),
            write_supabase=not estimate_only,
            progress_callback=progress_callback,
            source=source,
            source_table=source_table,
            where=where,
            estimate_only=bool(estimate_only),
            writeback=bool(writeback) and not estimate_only,
            verify_only=bool(verify_only),
            find_people=find_people,
            find_email=find_email,
            find_phone=find_phone,
            skip_tiers=skip_tiers,
            approve_cost_usd=approve_cost_usd,
        )

    def _run(job: Any) -> dict[str, Any]:
        from mcp_server.jobs import update_job_progress

        def on_progress(snapshot: dict[str, Any]) -> None:
            update_job_progress(job.id, snapshot)

        return _run_enrich_or_raise(progress_callback=on_progress)

    def _run_enrich_or_raise(
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        try:
            return _run_enrich(progress_callback)
        except Exception as exc:
            _tool_error(exc)

    if estimate_only:
        return _json(_run_enrich_or_raise())
    if source not in (None, "", {}, []) or source_table not in (None, "", {}, []):
        rows_chars = 50_000
    else:
        rows_chars = (
            len(rows) if isinstance(rows, str) else len(json.dumps(rows, default=str))
        )
    if background and (_http_mode() or rows_chars > 2000):
        from mcp_server.jobs import start_job

        job = start_job(
            "enrich_waterfall",
            _run,
            meta={
                "need": need_norm,
                "max_tier": max_tier_n,
                "verify_only": bool(verify_only),
                "find_people": find_people,
                "find_email": find_email,
                "find_phone": find_phone,
                "client_tag": client.tag,
                "rows_chars": rows_chars,
                "source_table": _table_ref(source, source_table),
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
    return _json(_run_enrich_or_raise())


@mcp.tool(
    annotations=ToolAnnotations(
        title="Enrich one person (phone + LinkedIn + email)",
        readOnlyHint=False,
        openWorldHint=True,
        destructiveHint=False,
    )
)
def enrich_person(
    client_tag: str,
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    full_name: str = "",
    linkedin_url: str = "",
    company_name: str = "",
    domain: str = "",
    need: str = "both",
    max_tier: str = "fullenrich",
    write_supabase: bool = False,
    approve_cost_usd: float | None = 0.25,
    skip_tiers: str | list[str] | None = None,
) -> str:
    """Look up one prospect through the waterfall and return that compact hit.

    For ReplyHandler Slack cards. Returns email / phone / linkedin_url /
    website / source tiers — not a bulk payload dump. FullEnrich runs for
    both work email and cellphone when max_tier is fullenrich.
    Default write_supabase=false so a Slack-card lookup does not create
    {tag}_wf_* rows unless asked.
    """
    _ensure_repo_cwd()
    _reload_settings()
    from email_waterfall import waterfall as wf

    try:
        return _json(
            wf.enrich_one_person(
                client_tag=client_tag,
                email=email,
                first_name=first_name,
                last_name=last_name,
                full_name=full_name,
                linkedin_url=linkedin_url,
                company_name=company_name,
                domain=domain,
                need=need,
                max_tier=max_tier,
                write_supabase=bool(write_supabase),
                approve_cost_usd=approve_cost_usd,
                skip_tiers=skip_tiers,
            )
        )
    except Exception as exc:
        _tool_error(exc)


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
            "Claude custom connector URL: /mcp\n"
            "Health: /health\n"
            "Auth: none\n"
        )

    @mcp.custom_route("/health", methods=["GET"])
    async def health_live(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "service": "email-waterfall",
                "transport": "streamable-http",
                "mcp_path": "/mcp",
                "auth": "none",
                "claude_web": (
                    "Add this connector in Claude → Settings → Connectors: "
                    "https://<host>/mcp"
                ),
            }
        )

    @mcp.custom_route("/enrich-one", methods=["POST"])
    async def enrich_one_http(request: Request) -> JSONResponse:
        """ReplyHandler one-person lookup. Same as the enrich_person MCP tool."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "reason": "invalid_json"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "reason": "invalid_json"}, status_code=400)
        tag = str(body.get("client_tag") or "").strip()
        if not tag:
            return JSONResponse(
                {"ok": False, "reason": "client_tag is required"}, status_code=400
            )
        _ensure_repo_cwd()
        _reload_settings()
        from email_waterfall import waterfall as wf

        try:
            hit = wf.enrich_one_person(
                client_tag=tag,
                email=str(body.get("email") or ""),
                first_name=str(body.get("first_name") or ""),
                last_name=str(body.get("last_name") or ""),
                full_name=str(body.get("full_name") or body.get("name") or ""),
                linkedin_url=str(body.get("linkedin_url") or ""),
                company_name=str(body.get("company_name") or ""),
                domain=str(body.get("domain") or ""),
                need=str(body.get("need") or "both"),
                max_tier=str(body.get("max_tier") or "fullenrich"),
                write_supabase=bool(body.get("write_supabase") or False),
                approve_cost_usd=body.get("approve_cost_usd"),
                skip_tiers=body.get("skip_tiers"),
            )
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "reason": f"{type(exc).__name__}: {exc}"},
                status_code=400,
            )
        return JSONResponse(hit)


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
