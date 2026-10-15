"""GetLeads MCP mapping, error logging, and waterfall warnings."""

from __future__ import annotations

import logging

from pathlib import Path

from email_waterfall.vendors.getleads import (
    DECISION_MAKERS_TOOL,
    GetLeadsClient,
    PERSON_ENRICH_TOOL,
    arguments_valid,
    collapse_company_identifiers,
    contact_title_matches,
    is_identifier_validation_error,
    is_rate_limit_error,
    items_ready,
    map_arguments,
    normalize_decision_maker,
    people_lookup_values,
    pick_satisfiable_tool,
    pick_tool,
    route_email_lookup,
    score_email_tool,
    score_people_tool,
    unwrap_decision_makers,
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

LOOKUP_DM_TOOL = {
    "name": "lookup_decision_makers",
    "description": (
        "Find decision makers at a company. Provide exactly one of domain, "
        "company_name, or linkedin_company_url."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "domain": {"type": "string"},
            "company_name": {"type": "string"},
            "linkedin_company_url": {"type": "string"},
            "limit": {"type": "integer"},
            "offset": {"type": "integer"},
        },
        "additionalProperties": False,
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
    def __init__(self, result=None, error=None, tools=None, errors=None):
        self.result = result
        self.error = error
        self.tools = tools
        self._error_queue = list(errors or [])
        self.calls: list[tuple[str, dict]] = []

    def list_tools(self):
        return self.tools if self.tools is not None else TOOLS

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name in (
            "getleads_get_emails_from_linkedin_batch",
            "getleads_enrich_person_batch",
        ):
            items = arguments.get("items")
            if not isinstance(items, list):
                raise McpError(
                    f'Invalid arguments for tool {name}: '
                    'expected array at "items", received undefined',
                    status=-32602,
                    body='{"error":{"code":-32602,"message":"items expected array, received undefined"}}',
                )
            if not items:
                raise McpError(
                    f"Invalid arguments for tool {name}: items must be a non-empty array",
                    status=-32602,
                    body='{"error":{"code":-32602,"message":"items expected array"}}',
                )
            if name == "getleads_get_emails_from_linkedin_batch" and not all(
                isinstance(row, dict) and row.get("linkedin_url") for row in items
            ):
                raise McpError(
                    "Invalid arguments for tool getleads_get_emails_from_linkedin_batch",
                    status=-32602,
                )
            if name == "getleads_enrich_person_batch" and not all(
                isinstance(row, dict)
                and row.get("first_name")
                and row.get("last_name")
                for row in items
            ):
                raise McpError(
                    "Invalid arguments for tool getleads_enrich_person_batch",
                    status=-32602,
                )
        if name == "lookup_decision_makers":
            ident = [
                key
                for key in ("domain", "company_name", "linkedin_company_url")
                if arguments.get(key)
            ]
            if len(ident) != 1:
                raise McpError(
                    "Invalid arguments for tool lookup_decision_makers: "
                    "Provide exactly one of domain, company_name, or "
                    "linkedin_company_url",
                    status=-32602,
                )
        if self._error_queue:
            raise self._error_queue.pop(0)
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
    assert mcp.calls[0][0] == PERSON_ENRICH_TOOL
    args = mcp.calls[0][1]
    assert args["items"] == [
        {
            "first_name": "Jane",
            "last_name": "Doe",
            "email_domain": "acme.test",
            "company_name": "Acme",
        }
    ]
    assert client.credits_charged == 1


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
        "items": [{"linkedin_url": "https://www.linkedin.com/in/jane-doe"}],
        "limit_per_item": 1,
    }
    assert client.calls == 1
    assert client.errors == 0


def test_find_email_skips_linkedin_batch_without_url() -> None:
    mcp = FakeMcp(result=ANON_EMAIL, tools=[LINKEDIN_BATCH_TOOL])
    client = GetLeadsClient(
        token_manager=Tok(), mcp=mcp, tools=[LINKEDIN_BATCH_TOOL]
    )
    hit = client.find_email("Jane", "Doe", "acme.test", "Acme")
    assert hit is not None
    assert mcp.calls[0][0] == PERSON_ENRICH_TOOL
    assert client.calls == 1
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


def test_route_linkedin_vs_person_vs_skip() -> None:
    tool, payload = route_email_lookup(
        linkedin_url="https://www.linkedin.com/in/jane-doe",
        first_name="Jane",
        last_name="Doe",
        domain="acme.test",
    )
    assert tool == "getleads_get_emails_from_linkedin_batch"
    assert items_ready(payload)
    assert payload["items"] == [{"linkedin_url": "https://www.linkedin.com/in/jane-doe"}]
    assert payload["limit_per_item"] == 1

    tool, payload = route_email_lookup(
        first_name="Jane", last_name="Doe", company_name="Acme"
    )
    assert tool == PERSON_ENRICH_TOOL
    assert payload["items"][0]["company_name"] == "Acme"
    assert "email_domain" not in payload["items"][0]

    tool, payload = route_email_lookup(first_name="Jane", last_name="Doe")
    assert tool is None
    assert payload is None
    assert not items_ready(payload)
    assert not items_ready({})
    assert not items_ready({"items": None})


def test_find_email_skips_when_neither_linkedin_nor_name() -> None:
    mcp = FakeMcp(result=ANON_EMAIL, tools=[LINKEDIN_BATCH_TOOL, PERSON_BATCH_TOOL])
    client = GetLeadsClient(
        token_manager=Tok(),
        mcp=mcp,
        tools=[LINKEDIN_BATCH_TOOL, PERSON_BATCH_TOOL],
    )
    hit = client.find_email("", "", "", "")
    assert hit is None
    assert mcp.calls == []
    assert client.calls == 0
    assert client.errors == 0


def test_find_email_name_company_without_domain() -> None:
    mcp = FakeMcp(result=ANON_EMAIL, tools=[LINKEDIN_BATCH_TOOL, PERSON_BATCH_TOOL])
    client = GetLeadsClient(
        token_manager=Tok(),
        mcp=mcp,
        tools=[LINKEDIN_BATCH_TOOL, PERSON_BATCH_TOOL],
    )
    hit = client.find_email("Jane", "Doe", "", "Helping Hands Inc")
    assert hit is not None
    assert mcp.calls[0][0] == PERSON_ENRICH_TOOL
    assert mcp.calls[0][1]["items"] == [
        {
            "first_name": "Jane",
            "last_name": "Doe",
            "company_name": "Helping Hands Inc",
        }
    ]
    assert client.credits_charged == 1


def test_find_email_bulk_person_batches_50() -> None:
    rows = [
        {"first_name": f"A{i}", "last_name": "Bee", "domain": "acme.test"}
        for i in range(51)
    ]
    mcp = FakeMcp(
        result={"results": [ANON_EMAIL]},
        tools=[PERSON_BATCH_TOOL],
    )
    client = GetLeadsClient(token_manager=Tok(), mcp=mcp, tools=[PERSON_BATCH_TOOL])
    hits = client.find_email_bulk(rows)
    assert len(mcp.calls) == 2
    assert mcp.calls[0][0] == PERSON_ENRICH_TOOL
    assert len(mcp.calls[0][1]["items"]) == 50
    assert len(mcp.calls[1][1]["items"]) == 1
    assert all(isinstance(mcp.calls[0][1]["items"], list) for _ in (0,))
    assert hits[0] is not None
    assert client.calls == 2


def test_invoke_never_sends_undefined_items() -> None:
    mcp = FakeMcp(result=ANON_EMAIL, tools=[LINKEDIN_BATCH_TOOL])
    client = GetLeadsClient(
        token_manager=Tok(), mcp=mcp, tools=[LINKEDIN_BATCH_TOOL]
    )
    assert client._invoke_tool(LINKEDIN_BATCH_TOOL["name"], {}) is None
    assert client._invoke_tool(LINKEDIN_BATCH_TOOL["name"], {"items": None}) is None
    assert client._invoke_tool(LINKEDIN_BATCH_TOOL["name"], {"items": []}) is None
    assert mcp.calls == []
    assert client.calls == 0
    assert client.errors == 0


def test_people_lookup_values_domain_wins() -> None:
    assert people_lookup_values("acme.com", "Acme Inc") == {"domain": "acme.com"}
    assert people_lookup_values("acme.com", "") == {"domain": "acme.com"}
    assert people_lookup_values("", "Acme Inc") == {"company_name": "Acme Inc"}
    assert people_lookup_values("", "") == {}
    assert people_lookup_values("  ", "  Acme  ") == {"company_name": "Acme"}


def test_collapse_company_identifiers_keeps_domain() -> None:
    assert collapse_company_identifiers(
        {"domain": "acme.com", "company_name": "Acme", "limit": 10}
    ) == {"domain": "acme.com", "limit": 10}
    assert collapse_company_identifiers({"company_name": "Acme"}) == {
        "company_name": "Acme"
    }


def test_find_people_sends_domain_only_when_both_present() -> None:
    mcp = FakeMcp(result=ANON_PEOPLE, tools=[LOOKUP_DM_TOOL])
    client = GetLeadsClient(token_manager=Tok(), mcp=mcp, tools=[LOOKUP_DM_TOOL])
    people = client.find_people(
        "perelson.com", company_name="Perelson", titles=["Owner"]
    )
    assert len(people) == 1
    assert mcp.calls[0][0] == DECISION_MAKERS_TOOL
    args = mcp.calls[0][1]
    assert args["domain"] == "perelson.com"
    assert "company_name" not in args
    assert "linkedin_company_url" not in args
    assert args["limit"] == 10
    assert client.errors == 0


def test_find_people_sends_company_name_only_without_domain() -> None:
    mcp = FakeMcp(result=ANON_PEOPLE, tools=[LOOKUP_DM_TOOL])
    client = GetLeadsClient(token_manager=Tok(), mcp=mcp, tools=[LOOKUP_DM_TOOL])
    people = client.find_people("", company_name="Perelson")
    assert len(people) == 1
    args = mcp.calls[0][1]
    assert args == {"company_name": "Perelson", "limit": 10}
    assert client.errors == 0


def test_find_people_skips_when_no_identifier() -> None:
    mcp = FakeMcp(result=ANON_PEOPLE, tools=[LOOKUP_DM_TOOL])
    client = GetLeadsClient(token_manager=Tok(), mcp=mcp, tools=[LOOKUP_DM_TOOL])
    people = client.find_people("", company_name="")
    assert people == []
    assert mcp.calls == []
    assert client.calls == 0
    assert client.errors == 0


def test_pick_people_tool_prefers_lookup_decision_makers() -> None:
    tool = pick_tool([PEOPLE_TOOL, LOOKUP_DM_TOOL, SEARCH_TOOL], score_people_tool)
    assert tool is not None
    assert tool["name"] == DECISION_MAKERS_TOOL


def test_identifier_validation_error_text() -> None:
    assert is_identifier_validation_error(
        "Provide exactly one of domain, company_name, or linkedin_company_url"
    )
    assert not is_identifier_validation_error("items expected array, received undefined")
    assert not is_identifier_validation_error("")


def _recorded_dm_contacts() -> dict:
    import json

    path = Path(__file__).parent / "fixtures" / "getleads_lookup_decision_makers.json"
    return json.loads(path.read_text())


def test_unwrap_decision_makers_reads_contacts_not_items_or_results() -> None:
    recorded = _recorded_dm_contacts()
    decoy = {
        "contacts": recorded["contacts"],
        "items": [{"first_name": "Nope", "current_title": "Intern"}],
        "results": [{"first_name": "Nope", "current_title": "Intern"}],
    }
    rows = unwrap_decision_makers(decoy)
    assert len(rows) == 3
    assert [r["first_name"] for r in rows] == ["Ann", "Ben", "Cara"]
    assert unwrap_decision_makers({"results": recorded["contacts"]}) == []
    assert unwrap_decision_makers({"items": [{"foo": 1}]}) == []


def test_normalize_decision_maker_maps_current_title_and_work_email() -> None:
    mapped = normalize_decision_maker(_recorded_dm_contacts()["contacts"][0])
    assert mapped["job_title"] == "Owner"
    assert mapped["title"] == "Owner"
    assert mapped["email"] == "ann@acme.test"
    assert mapped["phone"] == "+15551111111"
    assert mapped["domain"] == "acme.test"
    assert contact_title_matches(mapped["current_title"], ["Owner", "CEO"])


def test_find_people_parses_recorded_contacts_and_logs_title_match(caplog) -> None:
    recorded = _recorded_dm_contacts()
    mcp = FakeMcp(result=recorded, tools=[LOOKUP_DM_TOOL])
    client = GetLeadsClient(token_manager=Tok(), mcp=mcp, tools=[LOOKUP_DM_TOOL])
    with caplog.at_level(logging.INFO, logger="email_waterfall.vendors.getleads"):
        people = client.find_people("acme.test", titles=["Owner", "President", "CEO"])
    assert len(people) == 3
    assert [p.title for p in people] == ["Owner", "President", "CEO"]
    assert [p.email for p in people] == [
        "ann@acme.test",
        "ben@acme.test",
        "cara@acme.test",
    ]
    assert client.last_contacts_returned == 3
    assert client.last_title_matched == 3
    assert any("contacts=3" in rec.message for rec in caplog.records)
    assert any("title_matched=3" in rec.message for rec in caplog.records)


def test_recorded_getleads_contacts_write_three_rows(monkeypatch) -> None:
    from email_waterfall import waterfall
    from tests.test_waterfall import _patch_clients, _patch_writes, _vendor

    recorded = _recorded_dm_contacts()
    mcp = FakeMcp(result=recorded, tools=[LOOKUP_DM_TOOL])
    gl = GetLeadsClient(token_manager=Tok(), mcp=mcp, tools=[LOOKUP_DM_TOOL])
    sink: dict = {}
    _patch_clients(
        monkeypatch,
        gl=gl,
        ark=_vendor(enabled=False),
        fe=_vendor(enabled=False),
    )
    _patch_writes(monkeypatch, sink)

    out = waterfall.enrich_waterfall(
        [{"domain": "acme.test", "company_name": "Acme"}],
        client_tag="goliath",
        need="dm",
        max_tier="getleads",
        target_titles=["Owner", "President", "CEO"],
        require_title_match=True,
        write_supabase=True,
        parallel=False,
    )
    assert out["contacts_written"] == 3
    assert out["dms_found"] == 1
    assert out["tier_stats"]["getleads"]["dm_hits"] == 3
    assert out["tier_stats"]["getleads"]["email_hits"] == 3
    assert sink["contacts_table"] == "goliath_wf_contacts"
    assert len(sink["contacts"]) == 3
    assert {c["first_name"] for c in sink["contacts"]} == {"Ann", "Ben", "Cara"}
    assert all(c["source_tier"] == "getleads" for c in sink["contacts"])
    assert all(c["email_status"] == "VALID" for c in sink["contacts"])
    assert {c["email"] for c in sink["contacts"]} == {
        "ann@acme.test",
        "ben@acme.test",
        "cara@acme.test",
    }
    assert {c["job_title"] for c in sink["contacts"]} == {"Owner", "President", "CEO"}


def test_too_many_requests_retries_and_is_not_an_error(monkeypatch) -> None:
    monkeypatch.setattr(
        "email_waterfall.vendors.getleads.time.sleep", lambda _s: None
    )
    recorded = _recorded_dm_contacts()
    err = McpError("Too many requests", status=429, body="Too many requests")
    mcp = FakeMcp(
        result=recorded,
        tools=[LOOKUP_DM_TOOL],
        errors=[err, err],
    )
    client = GetLeadsClient(token_manager=Tok(), mcp=mcp, tools=[LOOKUP_DM_TOOL])
    people = client.find_people("acme.test", titles=["Owner"])
    assert len(people) == 3
    assert client.errors == 0
    assert client.calls == 1
    assert len(mcp.calls) == 3
    assert client.first_error is None


def test_rate_limit_does_not_trip_circuit(monkeypatch) -> None:
    from email_waterfall import waterfall
    from tests.test_waterfall import _patch_clients, _patch_writes, _vendor

    monkeypatch.setattr(
        "email_waterfall.vendors.getleads.time.sleep", lambda _s: None
    )
    err = McpError("Too many requests", status=429, body="Too many requests")
    mcp = FakeMcp(error=err, tools=[LOOKUP_DM_TOOL])
    gl = GetLeadsClient(token_manager=Tok(), mcp=mcp, tools=[LOOKUP_DM_TOOL])
    _patch_clients(
        monkeypatch,
        gl=gl,
        ark=_vendor(enabled=False),
        fe=_vendor(enabled=False),
    )
    _patch_writes(monkeypatch, {})
    rows = [{"domain": f"acme{i}.test", "company_name": "Acme"} for i in range(25)]
    out = waterfall.enrich_waterfall(
        rows,
        client_tag="goliath",
        need="dm",
        max_tier="getleads",
        write_supabase=True,
        parallel=False,
    )
    assert gl.errors == 0
    assert out["tier_stats"]["getleads"].get("disabled") is not True
    assert gl.calls == 25


def test_title_miss_writeback_is_title_miss(monkeypatch) -> None:
    from email_waterfall import waterfall
    from tests.test_waterfall import _patch_clients, _patch_writes, _vendor

    recorded = {
        "contacts": [
            {
                "first_name": "Sam",
                "last_name": "Seller",
                "current_title": "Sales Manager",
                "work_email": "sam@acme.test",
                "email_status": "VALID",
                "linkedin_url": "",
                "company_domain": "acme.test",
                "mobile_phone": "",
            }
        ]
    }
    mcp = FakeMcp(result=recorded, tools=[LOOKUP_DM_TOOL])
    gl = GetLeadsClient(token_manager=Tok(), mcp=mcp, tools=[LOOKUP_DM_TOOL])
    writebacks: list[dict] = []

    def fake_writeback(src, **kwargs):
        writebacks.append(kwargs)

    _patch_clients(
        monkeypatch,
        gl=gl,
        ark=_vendor(enabled=False),
        fe=_vendor(enabled=False),
    )
    _patch_writes(monkeypatch, {})
    miss_rows = [{"_source_key": 11, "domain": "acme.test", "company_name": "Acme"}]
    monkeypatch.setattr(
        waterfall.table_source,
        "fetch_source_rows",
        lambda src: list(miss_rows),
    )
    monkeypatch.setattr(waterfall.table_source, "ensure_writeback_columns", lambda src: [])
    monkeypatch.setattr(waterfall.table_source, "writeback_result", fake_writeback)
    monkeypatch.setattr(
        waterfall.table_source,
        "claim_source_rows",
        lambda src, *, owner, n=1, lease_seconds=900: [miss_rows.pop(0)] if miss_rows else [],
    )
    monkeypatch.setattr(waterfall.table_source, "release_claims", lambda *a, **k: 0)

    out = waterfall.enrich_waterfall(
        client_tag="goliath",
        need="dm",
        max_tier="getleads",
        target_titles=["Owner", "CEO"],
        require_title_match=True,
        write_supabase=True,
        parallel=False,
        source={
            "project_id": "kemvxzhcxvynmoutwdrh",
            "table": "ew_names_ready",
            "where": "wf_status is null",
        },
    )
    assert out["contacts_written"] == 0
    assert gl.last_contacts_returned == 1
    assert gl.last_title_matched == 0
    assert writebacks
    assert writebacks[0]["status"] == "title_miss"
    assert writebacks[0]["status"] != "found"
    assert writebacks[0]["status"] != "not_found"


def test_zero_contacts_writeback_is_not_found(monkeypatch) -> None:
    from email_waterfall import waterfall
    from tests.test_waterfall import _patch_clients, _patch_writes, _vendor

    mcp = FakeMcp(result={"contacts": []}, tools=[LOOKUP_DM_TOOL])
    gl = GetLeadsClient(token_manager=Tok(), mcp=mcp, tools=[LOOKUP_DM_TOOL])
    writebacks: list[dict] = []

    def fake_writeback(src, **kwargs):
        writebacks.append(kwargs)

    _patch_clients(
        monkeypatch,
        gl=gl,
        ark=_vendor(enabled=False),
        fe=_vendor(enabled=False),
    )
    _patch_writes(monkeypatch, {})
    empty_rows = [{"_source_key": 12, "domain": "empty.test", "company_name": "Empty"}]
    monkeypatch.setattr(
        waterfall.table_source,
        "fetch_source_rows",
        lambda src: list(empty_rows),
    )
    monkeypatch.setattr(waterfall.table_source, "ensure_writeback_columns", lambda src: [])
    monkeypatch.setattr(waterfall.table_source, "writeback_result", fake_writeback)
    monkeypatch.setattr(
        waterfall.table_source,
        "claim_source_rows",
        lambda src, *, owner, n=1, lease_seconds=900: [empty_rows.pop(0)] if empty_rows else [],
    )
    monkeypatch.setattr(waterfall.table_source, "release_claims", lambda *a, **k: 0)

    waterfall.enrich_waterfall(
        client_tag="goliath",
        need="dm",
        max_tier="getleads",
        write_supabase=True,
        parallel=False,
        source={
            "project_id": "kemvxzhcxvynmoutwdrh",
            "table": "ew_names_ready",
            "where": "wf_status is null",
        },
    )
    assert writebacks
    assert writebacks[0]["status"] == "not_found"


def test_writeback_status_found_title_miss_not_found() -> None:
    from email_waterfall._wf.row import _writeback_status

    assert (
        _writeback_status(
            {"_contacts_written": 3, "row": {"_getleads_contacts_returned": 3}}
        )
        == "found"
    )
    assert (
        _writeback_status(
            {"_contacts_written": 0, "people": [], "row": {"_getleads_contacts_returned": 4}}
        )
        == "title_miss"
    )
    assert (
        _writeback_status(
            {"_contacts_written": 0, "people": [], "row": {"_getleads_contacts_returned": 0}}
        )
        == "not_found"
    )


def test_is_rate_limit_error_text() -> None:
    assert is_rate_limit_error(McpError("Too many requests", status=429))
    assert is_rate_limit_error("rate limit exceeded")
    assert not is_rate_limit_error("items expected array, received undefined")
    assert not is_identifier_validation_error("Too many requests")
