"""GetLeads MCP mapping, error logging, and waterfall warnings."""

from __future__ import annotations

import logging

from email_waterfall.vendors.getleads import (
    GetLeadsClient,
    arguments_valid,
    map_arguments,
    pick_satisfiable_tool,
    score_email_tool,
)
from email_waterfall.vendors.mcp_http import McpError

EMAIL_TOOL = {
    "name": "find_work_email",
    "description": "Find a work email for this name and company domain",
    "inputSchema": {
        "type": "object",
        "properties": {
            "firstName": {"type": "string"},
            "lastName": {"type": "string"},
            "companyDomain": {"type": "string"},
        },
    },
}

PEOPLE_TOOL = {
    "name": "people_at_company",
    "description": "List people at a company by domain filtered by title",
    "inputSchema": {
        "type": "object",
        "properties": {
            "domain": {"type": "string"},
            "job_titles": {"type": "array"},
        },
    },
}

SEARCH_TOOL = {
    "name": "search_leads",
    "description": "Search leads by industry, location, seniority, and headcount",
    "inputSchema": {
        "type": "object",
        "properties": {
            "industry": {"type": "string"},
            "location": {"type": "array"},
            "seniority": {"type": "string"},
            "headcount": {"type": "string"},
            "title": {"type": "string"},
            "page": {"type": "integer"},
            "limit": {"type": "integer"},
        },
    },
}

LINKEDIN_BATCH_TOOL = {
    "name": "getleads_get_emails_from_linkedin_batch",
    "description": "Find work emails from LinkedIn profile URLs",
    "inputSchema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {"linkedin_url": {"type": "string", "minLength": 1}},
                    "required": ["linkedin_url"],
                    "additionalProperties": False,
                },
            },
            "limit_per_item": {"type": "integer"},
        },
        "required": ["items"],
        "additionalProperties": False,
    },
}

PERSON_BATCH_TOOL = {
    "name": "getleads_enrich_person_batch",
    "description": "Enrich a person by name and company",
    "inputSchema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "first_name": {"type": "string"},
                        "last_name": {"type": "string"},
                        "company_name": {"type": "string"},
                        "email_domain": {"type": "string"},
                    },
                    "required": ["first_name", "last_name"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    },
}

TOOLS = [EMAIL_TOOL, PEOPLE_TOOL, SEARCH_TOOL]

ANON_EMAIL = {
    "email": "jane@acme.test",
    "mobile": "+15551212000",
    "status": "valid",
    "first_name": "Jane",
    "last_name": "Doe",
}

ANON_PEOPLE = {
    "people": [
        {
            "first_name": "Pat",
            "last_name": "Owner",
            "title": "Owner",
            "email": "pat@perelson.com",
            "linkedin_url": "https://linkedin.com/in/pat-owner",
            "phone": "+18015551212",
            "company_name": "Perelson",
            "domain": "perelson.com",
        }
    ]
}


class Tok:
    auth_failed = False
    auth_failed_reason = None
    client_id = "cid"
    has_refresh_token = True
    has_api_key = False


class FakeMcp:
    def __init__(self, result=None, error=None, tools=None):
        self.result = result
        self.error = error
        self.tools = tools
        self.calls: list[tuple[str, dict]] = []

    def list_tools(self):
        return self.tools if self.tools is not None else TOOLS

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "getleads_get_emails_from_linkedin_batch":
            items = arguments.get("items")
            if not isinstance(items, list):
                raise McpError(
                    'Invalid arguments for tool getleads_get_emails_from_linkedin_batch: '
                    'expected array at "items", received undefined',
                    status=-32602,
                )
            if not items or not all(
                isinstance(row, dict) and row.get("linkedin_url") for row in items
            ):
                raise McpError(
                    "Invalid arguments for tool getleads_get_emails_from_linkedin_batch",
                    status=-32602,
                )
        if self.error is not None:
            raise self.error
        return self.result


def _client(mcp: FakeMcp) -> GetLeadsClient:
    return GetLeadsClient(token_manager=Tok(), mcp=mcp, tools=TOOLS)


def test_find_email_maps_real_tool_shape() -> None:
    mcp = FakeMcp(result=ANON_EMAIL)
    client = _client(mcp)
    hit = client.find_email("Jane", "Doe", "acme.test", "Acme")
    assert hit is not None
    assert hit.email == "jane@acme.test"
    assert hit.phone == "+15551212000"
    assert hit.source_tier == "getleads"
    assert mcp.calls[0][0] == "find_work_email"
    args = mcp.calls[0][1]
    assert args["firstName"] == "Jane"
    assert args["lastName"] == "Doe"
    assert args["companyDomain"] == "acme.test"


def test_find_people_maps_real_tool_shape() -> None:
    mcp = FakeMcp(result=ANON_PEOPLE)
    client = _client(mcp)
    people = client.find_people("perelson.com", titles=["Owner", "Founder", "CEO"])
    assert len(people) == 1
    assert people[0].first_name == "Pat"
    assert people[0].title == "Owner"
    assert people[0].phone == "+18015551212"
    assert people[0].email == "pat@perelson.com"
    args = mcp.calls[0][1]
    assert args["domain"] == "perelson.com"
    assert args["job_titles"] == ["Owner", "Founder", "CEO"]


def test_search_people_passthrough() -> None:
    mcp = FakeMcp(
        result={
            "people": ANON_PEOPLE["people"],
            "total": 12,
            "next_page": 1,
        }
    )
    client = _client(mcp)
    out = client.search_people(
        {
            "industry": "staffing",
            "location": ["Utah"],
            "seniority": "owner",
            "title": "Owner",
        },
        page=0,
        size=25,
    )
    assert out["total"] == 12
    assert out["next_page"] == 1
    assert len(out["people"]) == 1
    args = mcp.calls[0][1]
    assert args["industry"] == "staffing"
    assert args["location"] == ["Utah"]
    assert args["limit"] == 25


def test_health_snapshot_api_key_mode() -> None:
    class KeyTok:
        auth_failed = False
        auth_failed_reason = None
        client_id = ""
        has_refresh_token = False
        has_api_key = True

    client = GetLeadsClient(token_manager=KeyTok(), mcp=FakeMcp(), tools=TOOLS)
    assert client.enabled is True
    snap = client.health_snapshot()
    assert snap["configured"] is True
    assert snap["auth_ok"] is True
    assert snap["auth"] == "api_key"
    assert snap["tools"] == 3


def test_http_404_logs_warning_and_increments_errors(caplog) -> None:
    err = McpError("not found", status=404, body="This page could not be found")
    mcp = FakeMcp(error=err)
    client = _client(mcp)
    with caplog.at_level(logging.WARNING, logger="email_waterfall.vendors"):
        hit = client.find_email("Jane", "Doe", "acme.test")
    assert hit is None
    assert client.errors == 1
    assert client.hits == 0
    assert any("status=404" in rec.message for rec in caplog.records)
    assert any("getleads" in rec.message for rec in caplog.records)


def test_http_500_is_error_not_silent_none(caplog) -> None:
    err = McpError("boom", status=500, body="internal")
    mcp = FakeMcp(error=err)
    client = _client(mcp)
    with caplog.at_level(logging.WARNING, logger="email_waterfall.vendors"):
        people = client.find_people("x.com")
    assert people == []
    assert client.errors == 1
    assert any("status=500" in rec.message for rec in caplog.records)


def test_waterfall_warnings_when_errors_dominate(monkeypatch) -> None:
    from email_waterfall import waterfall
    from tests.test_waterfall import _patch_clients, _patch_writes, _vendor

    class Noisy:
        enabled = True
        calls = 20
        hits = 0
        errors = 18

        def find_email(self, *args, **kwargs):
            return None

        def find_people(self, *args, **kwargs):
            return []

        def find_email_bulk(self, *args, **kwargs):
            return []

        def find_mobile(self, *args, **kwargs):
            return None

    noisy = Noisy()
    _patch_clients(
        monkeypatch,
        gl=noisy,
        ark=_vendor(enabled=False),
        lm=_vendor(enabled=False),
        fe=_vendor(enabled=False),
    )
    _patch_writes(monkeypatch, {})
    out = waterfall.enrich_waterfall(
        [
            {
                "domain": "acme.test",
                "first_name": "Jane",
                "last_name": "Doe",
                "title": "Owner",
            }
        ],
        client_tag="peterson",
        need="email",
        max_tier="getleads",
        write_supabase=True,
    )
    assert out["tier_stats"]["getleads"]["errors"] == 18
    assert "getleads" in out["warnings"]


def test_map_linkedin_batch_wraps_items() -> None:
    mapped = map_arguments(
        LINKEDIN_BATCH_TOOL,
        {"linkedin_url": "https://www.linkedin.com/in/jane-doe", "first_name": "Jane"},
    )
    assert mapped == {
        "items": [{"linkedin_url": "https://www.linkedin.com/in/jane-doe"}]
    }
    assert arguments_valid(LINKEDIN_BATCH_TOOL, mapped)


def test_map_linkedin_batch_without_url_is_invalid() -> None:
    mapped = map_arguments(
        LINKEDIN_BATCH_TOOL,
        {"first_name": "Jane", "last_name": "Doe", "domain": "acme.test"},
    )
    assert "items" not in mapped
    assert not arguments_valid(LINKEDIN_BATCH_TOOL, mapped)


def test_map_person_batch_wraps_items() -> None:
    mapped = map_arguments(
        PERSON_BATCH_TOOL,
        {
            "first_name": "Jane",
            "last_name": "Doe",
            "domain": "acme.test",
            "company_name": "Acme",
        },
    )
    assert mapped == {
        "items": [
            {
                "first_name": "Jane",
                "last_name": "Doe",
                "email_domain": "acme.test",
                "company_name": "Acme",
            }
        ]
    }
    assert arguments_valid(PERSON_BATCH_TOOL, mapped)


def test_find_email_linkedin_batch_sends_items() -> None:
    mcp = FakeMcp(result=ANON_EMAIL, tools=[LINKEDIN_BATCH_TOOL])
    client = GetLeadsClient(
        token_manager=Tok(), mcp=mcp, tools=[LINKEDIN_BATCH_TOOL]
    )
    hit = client.find_email(
        "Jane",
        "Doe",
        "acme.test",
        "Acme",
        linkedin_url="https://www.linkedin.com/in/jane-doe",
    )
    assert hit is not None
    assert hit.email == "jane@acme.test"
    assert mcp.calls[0][0] == "getleads_get_emails_from_linkedin_batch"
    assert mcp.calls[0][1] == {
        "items": [{"linkedin_url": "https://www.linkedin.com/in/jane-doe"}]
    }
    assert client.calls == 1
    assert client.errors == 0


def test_find_email_skips_linkedin_batch_without_url() -> None:
    mcp = FakeMcp(result=ANON_EMAIL, tools=[LINKEDIN_BATCH_TOOL])
    client = GetLeadsClient(
        token_manager=Tok(), mcp=mcp, tools=[LINKEDIN_BATCH_TOOL]
    )
    hit = client.find_email("Jane", "Doe", "acme.test", "Acme")
    assert hit is None
    assert mcp.calls == []
    assert client.calls == 0
    assert client.errors == 0


def test_find_email_falls_back_to_person_batch_without_linkedin() -> None:
    tools = [LINKEDIN_BATCH_TOOL, PERSON_BATCH_TOOL]
    mcp = FakeMcp(result=ANON_EMAIL, tools=tools)
    client = GetLeadsClient(token_manager=Tok(), mcp=mcp, tools=tools)
    hit = client.find_email("Jane", "Doe", "acme.test", "Acme")
    assert hit is not None
    assert mcp.calls[0][0] == "getleads_enrich_person_batch"
    assert mcp.calls[0][1]["items"][0]["first_name"] == "Jane"
    assert mcp.calls[0][1]["items"][0]["email_domain"] == "acme.test"
    assert client.errors == 0


def test_pick_satisfiable_prefers_linkedin_when_url_present() -> None:
    values = {
        "first_name": "Jane",
        "last_name": "Doe",
        "domain": "acme.test",
        "linkedin_url": "https://www.linkedin.com/in/jane-doe",
    }
    tool = pick_satisfiable_tool(
        [LINKEDIN_BATCH_TOOL, PERSON_BATCH_TOOL], score_email_tool, values
    )
    assert tool is not None
    assert tool["name"] == "getleads_get_emails_from_linkedin_batch"
