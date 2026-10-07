"""GetLeads — first waterfall tier via OAuth MCP (not REST).

Public interface is unchanged: find_email, find_people, enabled, calls, hits.
Tool names come from tools/list (schema scoring). Guessed REST paths are what
caused silent 404s; we never invent a tool name that isn't on the server.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from email_waterfall.config import settings
from email_waterfall.need import CAP_EMAIL, CAP_PEOPLE, assert_capability
from email_waterfall.vendors.base import EmailHit, PersonHit, person_from_row
from email_waterfall.vendors.errors import bump_errors, log_vendor_failure
from email_waterfall.vendors.mcp_http import McpError, McpHttpClient
from email_waterfall.vendors.oauth_token import OAuthTokenManager

log = logging.getLogger("email_waterfall.vendors.getleads")

_MISSING_EMAIL_LOGGED = False
_MISSING_PEOPLE_LOGGED = False
_MISSING_SEARCH_LOGGED = False
_GAP_LOCK = threading.Lock()

EMAIL_NAME_HINTS = (
    "find_email",
    "find-email",
    "email_finder",
    "email-finder",
    "get_email",
    "work_email",
    "enrich_person",
    "enrich_contact",
    "enrich_lead",
)
PEOPLE_NAME_HINTS = (
    "find_people",
    "company_people",
    "people_at",
    "employees",
    "decision_maker",
    "decision-maker",
)
SEARCH_NAME_HINTS = (
    "search_leads",
    "search_people",
    "search_person",
    "search_contacts",
    "find_leads",
    "query_leads",
    "lead_search",
)
NAME_KEYS = {"first_name", "firstname", "first", "given_name", "givenname"}
LAST_KEYS = {"last_name", "lastname", "last", "family_name", "familyname"}
DOMAIN_KEYS = {
    "domain",
    "company_domain",
    "companydomain",
    "website",
    "company_website",
    "email_domain",
    "emaildomain",
}
EMAIL_DOMAIN_KEYS = {
    "email_domain",
    "emaildomain",
    "domain",
    "company_domain",
    "companydomain",
    "website",
    "company_website",
}
COMPANY_KEYS = {"company_name", "companyname", "company", "organization"}
TITLE_KEYS = {"title", "titles", "job_title", "jobtitle", "job_titles", "jobtitles", "headline"}
LINKEDIN_KEYS = {
    "linkedin_url",
    "linkedin",
    "profile_url",
    "profileurl",
    "li_url",
    "linkedinurl",
}
INDUSTRY_KEYS = {"industry", "industries", "naics", "sic"}
GEO_KEYS = {"location", "locations", "state", "states", "geo", "country", "region", "city"}
SENIORITY_KEYS = {"seniority", "job_level", "joblevel", "management_level"}
HEADCOUNT_KEYS = {"headcount", "employee_count", "employees", "company_size", "size"}
ALIAS_GROUPS: dict[str, set[str]] = {
    "first_name": NAME_KEYS,
    "last_name": LAST_KEYS,
    "domain": DOMAIN_KEYS,
    "email_domain": EMAIL_DOMAIN_KEYS,
    "company_name": COMPANY_KEYS,
    "titles": TITLE_KEYS,
    "title": TITLE_KEYS,
    "linkedin_url": LINKEDIN_KEYS,
    "page": {"page", "page_index", "pageindex", "offset"},
    "size": {"size", "limit", "page_size", "pagesize", "per_page", "perpage", "count"},
    "industry": INDUSTRY_KEYS,
    "location": GEO_KEYS,
    "state": {"state", "states", "region"},
    "seniority": SENIORITY_KEYS,
    "headcount": HEADCOUNT_KEYS,
    "filters": {"filters", "filter", "query_filters"},
    "query": {"query", "q", "search", "prompt"},
    "items": {"items", "rows", "records", "batch"},
}


def _norm_key(key: str) -> str:
    return key.lower().replace("-", "_").replace(" ", "_")


def _input_schema(tool: dict[str, Any]) -> dict[str, Any]:
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    return schema if isinstance(schema, dict) else {}


def _schema_props(tool: dict[str, Any]) -> dict[str, Any]:
    props = _input_schema(tool).get("properties") or {}
    return props if isinstance(props, dict) else {}


def _item_object_schema(tool: dict[str, Any]) -> dict[str, Any] | None:
    """If the tool takes a batch under `items: [{...}]`, return the item object schema."""
    items = _schema_props(tool).get("items")
    if not isinstance(items, dict) or items.get("type") != "array":
        return None
    inner = items.get("items")
    if isinstance(inner, dict) and (inner.get("type") == "object" or inner.get("properties")):
        return inner
    return None


def _item_props(tool: dict[str, Any]) -> dict[str, Any]:
    item_schema = _item_object_schema(tool)
    if not item_schema:
        return {}
    props = item_schema.get("properties") or {}
    return props if isinstance(props, dict) else {}


def _prop_keys(tool: dict[str, Any]) -> set[str]:
    return {_norm_key(k) for k in list(_schema_props(tool)) + list(_item_props(tool))}


def _tool_name(tool: dict[str, Any]) -> str:
    return str(tool.get("name") or "")


def _tool_desc(tool: dict[str, Any]) -> str:
    return str(tool.get("description") or "").lower()


def score_email_tool(tool: dict[str, Any]) -> int:
    name = _tool_name(tool).lower()
    desc = _tool_desc(tool)
    keys = _prop_keys(tool)
    score = 0
    if any(h in name for h in EMAIL_NAME_HINTS):
        score += 6
    if "email" in name and "verif" not in name:
        score += 3
    has_first = bool(keys & NAME_KEYS)
    has_last = bool(keys & LAST_KEYS)
    has_domain = bool(keys & DOMAIN_KEYS)
    has_company = bool(keys & COMPANY_KEYS)
    if has_first and has_last and (has_domain or has_company):
        score += 8
    if bool(keys & LINKEDIN_KEYS) and "email" in name:
        score += 7
    if "email" in desc and any(w in desc for w in ("find", "enrich", "work")):
        score += 2
    if any(h in name for h in SEARCH_NAME_HINTS) and "email" not in name:
        score -= 6
    if "verif" in name:
        score -= 8
    return score


def score_people_tool(tool: dict[str, Any]) -> int:
    name = _tool_name(tool).lower()
    desc = _tool_desc(tool)
    keys = _prop_keys(tool)
    score = 0
    if any(h in name for h in PEOPLE_NAME_HINTS):
        score += 6
    if bool(keys & DOMAIN_KEYS):
        score += 5
    if bool(keys & TITLE_KEYS):
        score += 3
    if "people" in name or "employee" in name or "contact" in name:
        score += 2
    if "at this domain" in desc or "at a company" in desc or "by domain" in desc:
        score += 3
    # Global ICP search is a different method.
    if bool(keys & INDUSTRY_KEYS) or bool(keys & GEO_KEYS):
        score -= 3
    if any(h in name for h in SEARCH_NAME_HINTS):
        score -= 2
    if any(h in name for h in EMAIL_NAME_HINTS):
        score -= 4
    return score


def score_search_tool(tool: dict[str, Any]) -> int:
    name = _tool_name(tool).lower()
    desc = _tool_desc(tool)
    keys = _prop_keys(tool)
    score = 0
    if any(h in name for h in SEARCH_NAME_HINTS):
        score += 8
    if bool(keys & INDUSTRY_KEYS):
        score += 4
    if bool(keys & GEO_KEYS):
        score += 4
    if bool(keys & SENIORITY_KEYS):
        score += 3
    if bool(keys & HEADCOUNT_KEYS):
        score += 2
    if bool(keys & TITLE_KEYS):
        score += 2
    if "search" in desc and any(w in desc for w in ("lead", "people", "contact", "icp")):
        score += 2
    if any(h in name for h in EMAIL_NAME_HINTS):
        score -= 6
    return score


def pick_tool(tools: list[dict[str, Any]], scorer, *, min_score: int = 6) -> dict[str, Any] | None:
    ranked = sorted(((scorer(t), t) for t in tools), key=lambda x: x[0], reverse=True)
    if not ranked or ranked[0][0] < min_score:
        return None
    return ranked[0][1]


def _find_prop(props: dict[str, Any], candidates: set[str]) -> str | None:
    for key in props:
        if _norm_key(key) in candidates:
            return key
    return None


def _coerce_prop(dest_schema: dict[str, Any], value: Any) -> Any:
    dest_type = str(dest_schema.get("type") or "")
    if dest_type == "array" and not isinstance(value, list):
        return [value]
    if dest_type != "array" and isinstance(value, list):
        return value[0] if value else value
    return value


def _map_onto_props(
    props: dict[str, Any], values: dict[str, Any], *, used: set[str] | None = None
) -> dict[str, Any]:
    used = used if used is not None else set()
    out: dict[str, Any] = {}
    for our_key, value in values.items():
        if value in (None, "", [], {}):
            continue
        group = ALIAS_GROUPS.get(our_key, {_norm_key(our_key)})
        dest = _find_prop(props, group)
        if not dest or dest in used:
            continue
        dest_schema = props.get(dest) if isinstance(props.get(dest), dict) else {}
        out[dest] = _coerce_prop(dest_schema if isinstance(dest_schema, dict) else {}, value)
        used.add(dest)
    return out


def _wrap_items(tool: dict[str, Any], values: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Build `items: [{...}]` from a single row when the live schema requires it."""
    raw_items = values.get("items")
    if isinstance(raw_items, list) and raw_items:
        return [r for r in raw_items if isinstance(r, dict)]
    item_schema = _item_object_schema(tool)
    if not item_schema:
        return None
    item_props = _item_props(tool)
    if not item_props:
        return None
    item = _map_onto_props(item_props, values)
    return [item] if item else None


def map_arguments(tool: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Copy our values onto the tool's real property names. Skip unknown keys."""
    props = _schema_props(tool)
    if not props:
        # No schema advertised — pass values as-is so the server can reject them.
        return {k: v for k, v in values.items() if v not in (None, "", [], {})}
    out = _map_onto_props(props, values)
    items = _wrap_items(tool, values)
    items_key = _find_prop(props, {"items", "rows", "records", "batch"})
    if items_key and items:
        out[items_key] = items
    elif items_key and items_key in out and not isinstance(out[items_key], list):
        # Never send a scalar where the schema wants the batch array.
        out.pop(items_key, None)
    # Nested filters object: dump leftover ICP fields into it.
    filters_key = _find_prop(props, {"filters", "filter"})
    if filters_key and filters_key not in out:
        leftover = {
            k: v
            for k, v in values.items()
            if k not in ALIAS_GROUPS and v not in (None, "", [], {})
        }
        if leftover:
            out[filters_key] = leftover
    extra_ok = _input_schema(tool).get("additionalProperties", True)
    if extra_ok is False:
        out = {k: v for k, v in out.items() if k in props}
    return out


def arguments_valid(tool: dict[str, Any], arguments: dict[str, Any]) -> bool:
    """True when mapped args satisfy required fields on the live tools/list schema."""
    schema = _input_schema(tool)
    required = schema.get("required") or []
    if not isinstance(required, list):
        required = []
    for key in required:
        val = arguments.get(key)
        if val in (None, "", [], {}):
            return False
    item_schema = _item_object_schema(tool)
    if not item_schema:
        return True
    items = arguments.get("items")
    if not isinstance(items, list) or not items:
        return False
    item_required = item_schema.get("required") or []
    if not isinstance(item_required, list):
        item_required = []
    for item in items:
        if not isinstance(item, dict):
            return False
        for key in item_required:
            if item.get(key) in (None, "", [], {}):
                return False
    return True


def pick_satisfiable_tool(
    tools: list[dict[str, Any]],
    scorer,
    values: dict[str, Any],
    *,
    min_score: int = 6,
) -> dict[str, Any] | None:
    ranked = sorted(((scorer(t), t) for t in tools), key=lambda x: x[0], reverse=True)
    for score, tool in ranked:
        if score < min_score:
            break
        mapped = map_arguments(tool, values)
        if arguments_valid(tool, mapped):
            return tool
    return None


def _looks_like_person_dict(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    keys = {_norm_key(k) for k in row}
    return bool(
        keys
        & (
            NAME_KEYS
            | LAST_KEYS
            | {"full_name", "fullname", "name", "email", "linkedin", "linkedin_url"}
        )
    )


def unwrap_records(data: Any) -> list[dict[str, Any]]:
    if data is None:
        return []
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if not isinstance(data, dict):
        return []
    for key in (
        "people",
        "leads",
        "contacts",
        "results",
        "items",
        "data",
        "records",
        "hits",
    ):
        nested = data.get(key)
        if isinstance(nested, list):
            return [r for r in nested if isinstance(r, dict)]
        if isinstance(nested, dict) and _looks_like_person_dict(nested):
            return [nested]
        if isinstance(nested, dict):
            inner = unwrap_records(nested)
            if inner:
                return inner
    for key in ("person", "lead", "contact", "result"):
        nested = data.get(key)
        if isinstance(nested, dict) and _looks_like_person_dict(nested):
            return [nested]
    if _looks_like_person_dict(data):
        return [data]
    return []


def _flatten_person(row: dict[str, Any]) -> dict[str, Any]:
    flat = dict(row)
    for nest in ("person", "contact", "lead", "profile", "company"):
        inner = row.get(nest)
        if isinstance(inner, dict):
            for k, v in inner.items():
                flat.setdefault(k, v)
    return flat


def extract_email(data: dict[str, Any]) -> tuple[str, str]:
    candidates: list[Any] = [
        data.get("email"),
        data.get("work_email"),
        data.get("workEmail"),
        data.get("professional_email"),
        data.get("business_email"),
    ]
    person = data.get("person") if isinstance(data.get("person"), dict) else {}
    contact = data.get("contact") if isinstance(data.get("contact"), dict) else {}
    candidates.extend(
        [
            person.get("email"),
            contact.get("email"),
            (data.get("emails") or [None])[0] if isinstance(data.get("emails"), list) else None,
        ]
    )
    for raw in candidates:
        if isinstance(raw, dict):
            raw = raw.get("email") or raw.get("address") or raw.get("value")
        email = str(raw or "").strip().lower()
        if email and "@" in email and "*" not in email:
            return email, str(data.get("status") or "found")
    return "", ""


def extract_phone(data: dict[str, Any]) -> str:
    for key in ("phone", "mobile", "cellphone", "direct_dial", "directDial", "mobile_phone"):
        value = data.get(key)
        if isinstance(value, dict):
            value = value.get("number") or value.get("value") or value.get("phone")
        text = str(value or "").strip()
        if len("".join(c for c in text if c.isdigit())) >= 7:
            return text
    return ""


def extract_total(data: dict[str, Any], rows: list) -> int | None:
    for key in ("total", "totalElements", "total_results", "count", "totalCount", "matched"):
        val = data.get(key)
        if isinstance(val, bool) or val is None:
            continue
        try:
            return int(val)
        except (TypeError, ValueError):
            continue
    return None


def compact_person(person: PersonHit) -> dict[str, Any]:
    raw = _flatten_person(person.raw or {})
    company = raw.get("company_name") or raw.get("company") or raw.get("organization") or ""
    if isinstance(company, dict):
        company = company.get("name") or company.get("company_name") or ""
    domain = (
        raw.get("domain")
        or raw.get("company_domain")
        or raw.get("website")
        or ""
    )
    if isinstance(domain, dict):
        domain = domain.get("domain") or domain.get("name") or ""
    if person.email and "@" in person.email and not domain:
        domain = person.email.split("@", 1)[1]
    state = raw.get("state") or raw.get("region") or raw.get("location") or ""
    if isinstance(state, dict):
        state = state.get("state") or state.get("name") or ""
    return {
        "name": person.name,
        "title": person.title,
        "company": str(company or ""),
        "domain": str(domain or ""),
        "linkedin": person.linkedin_url or "",
        "state": str(state or ""),
        "email": bool(person.email),
        "phone": bool(person.phone),
    }


def extract_next_page(data: dict[str, Any], page: int, rows: list, size: int) -> int | None:
    for key in ("next_page", "nextPage"):
        val = data.get(key)
        if val in (None, "", False):
            continue
        try:
            return int(val)
        except (TypeError, ValueError):
            if val is True:
                return page + 1
    total = extract_total(data, rows)
    if total is not None and (page + 1) * size < total:
        return page + 1
    if size and len(rows) >= size:
        return page + 1
    return None


def _log_gap_once(flag_name: str, message: str) -> None:
    global _MISSING_EMAIL_LOGGED, _MISSING_PEOPLE_LOGGED, _MISSING_SEARCH_LOGGED
    with _GAP_LOCK:
        current = {
            "_MISSING_EMAIL_LOGGED": _MISSING_EMAIL_LOGGED,
            "_MISSING_PEOPLE_LOGGED": _MISSING_PEOPLE_LOGGED,
            "_MISSING_SEARCH_LOGGED": _MISSING_SEARCH_LOGGED,
        }[flag_name]
        if current:
            return
        if flag_name == "_MISSING_EMAIL_LOGGED":
            _MISSING_EMAIL_LOGGED = True
        elif flag_name == "_MISSING_PEOPLE_LOGGED":
            _MISSING_PEOPLE_LOGGED = True
        else:
            _MISSING_SEARCH_LOGGED = True
    log.info(message)


class GetLeadsClient:
    tier = "getleads"

    def __init__(
        self,
        *,
        token_manager: OAuthTokenManager | None = None,
        mcp: McpHttpClient | None = None,
        timeout: int = 45,
        tools: list[dict[str, Any]] | None = None,
    ):
        self.timeout = timeout
        self.calls = 0
        self.hits = 0
        self.errors = 0
        self._token = token_manager
        self._mcp = mcp
        self._tools = tools
        self._tools_lock = threading.Lock()
        if self._tools is None:
            self._warm_tools()

    def _warm_tools(self) -> None:
        """tools/list at client startup so later calls validate against the live schema."""
        try:
            if self.enabled:
                self.list_tools()
        except Exception as exc:
            log.info("getleads tools/list at startup failed: %s", exc)

    def _manager(self) -> OAuthTokenManager:
        if self._token is None:
            self._token = OAuthTokenManager("getleads")
        return self._token

    def _client(self) -> McpHttpClient:
        if self._mcp is None:
            self._mcp = McpHttpClient(
                url=settings.getleads_mcp_url,
                token_manager=self._manager(),
                tier=self.tier,
                timeout=self.timeout,
            )
        return self._mcp

    @property
    def enabled(self) -> bool:
        mgr = self._manager()
        return (
            bool(mgr.has_refresh_token or getattr(mgr, "has_api_key", False))
            and not mgr.auth_failed
        )

    def health_snapshot(self) -> dict[str, Any]:
        mgr = self._manager()
        has_key = bool(getattr(mgr, "has_api_key", False))
        configured = bool(mgr.has_refresh_token or has_key or mgr.client_id)
        auth_mode = "oauth" if mgr.has_refresh_token else ("api_key" if has_key else None)
        if mgr.auth_failed:
            return {
                "configured": configured,
                "auth_ok": False,
                "reason": mgr.auth_failed_reason,
                "auth": auth_mode,
                "tools": None,
            }
        if not mgr.has_refresh_token and not has_key:
            return {
                "configured": False,
                "auth_ok": False,
                "reason": "missing_refresh_token",
                "auth": None,
                "tools": None,
            }
        try:
            tools = self.list_tools()
            return {
                "configured": True,
                "auth_ok": True,
                "reason": None,
                "auth": auth_mode,
                "tools": len(tools),
            }
        except Exception as exc:
            return {
                "configured": True,
                "auth_ok": False,
                "reason": str(exc)[:200],
                "auth": auth_mode,
                "tools": None,
            }

    def list_tools(self) -> list[dict[str, Any]]:
        with self._tools_lock:
            if self._tools is not None:
                return list(self._tools)
        tools = self._client().list_tools()
        with self._tools_lock:
            self._tools = tools
        return list(tools)

    def search_filter_schema(self) -> dict[str, Any] | None:
        tool = pick_tool(self.list_tools(), score_search_tool)
        if not tool:
            return None
        return {
            "name": _tool_name(tool),
            "description": tool.get("description"),
            "inputSchema": tool.get("inputSchema") or tool.get("input_schema"),
        }

    def _call(self, tool: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any] | None:
        mapped = map_arguments(tool, arguments)
        if not arguments_valid(tool, mapped):
            return None
        self.calls += 1
        try:
            return self._client().call_tool(_tool_name(tool), mapped)
        except McpError as exc:
            bump_errors(self)
            log_vendor_failure(
                self.tier,
                settings.getleads_mcp_url,
                status=exc.status if exc.status is not None else "mcp",
                body=exc.body,
                error=str(exc),
            )
            return None
        except Exception as exc:
            bump_errors(self)
            log_vendor_failure(
                self.tier,
                settings.getleads_mcp_url,
                error=str(exc),
            )
            return None

    def find_email(
        self,
        first_name: str,
        last_name: str,
        domain: str,
        company_name: str = "",
        *,
        linkedin_url: str = "",
    ) -> EmailHit | None:
        assert_capability(CAP_EMAIL, vendor=self.tier, endpoint="mcp tools/call")
        if not self.enabled:
            return None
        linkedin_url = (linkedin_url or "").strip()
        values = {
            "first_name": first_name,
            "last_name": last_name,
            "domain": domain,
            "email_domain": domain,
            "company_name": company_name or domain,
            "linkedin_url": linkedin_url,
        }
        try:
            tools = self.list_tools()
        except Exception as exc:
            bump_errors(self)
            log_vendor_failure(self.tier, settings.getleads_mcp_url, error=str(exc))
            return None
        tool = pick_satisfiable_tool(tools, score_email_tool, values)
        if not tool:
            _log_gap_once(
                "_MISSING_EMAIL_LOGGED",
                "getleads has no tools/list entry covering find work email "
                "(or row lacks fields the live schema requires, e.g. linkedin_url); skipping find_email",
            )
            return None
        data = self._call(tool, values)
        if not data:
            return None
        email, status = extract_email(data)
        if not email:
            records = unwrap_records(data)
            if records:
                email, status = extract_email(_flatten_person(records[0]))
        if not email:
            return None
        phone = extract_phone(data)
        if not phone and unwrap_records(data):
            phone = extract_phone(_flatten_person(unwrap_records(data)[0]))
        self.hits += 1
        return EmailHit(
            email=email,
            source_tier=self.tier,
            status=status or "found",
            phone=phone,
            raw=data,
        )

    def find_people(
        self,
        domain: str,
        *,
        company_name: str = "",
        titles: list[str] | None = None,
        limit: int = 10,
    ) -> list[PersonHit]:
        assert_capability(CAP_PEOPLE, vendor=self.tier, endpoint="mcp tools/call")
        if not self.enabled:
            return []
        try:
            tools = self.list_tools()
        except Exception as exc:
            bump_errors(self)
            log_vendor_failure(self.tier, settings.getleads_mcp_url, error=str(exc))
            return []
        tool = pick_tool(tools, score_people_tool)
        if not tool:
            _log_gap_once(
                "_MISSING_PEOPLE_LOGGED",
                "getleads has no tools/list entry covering people at a domain; skipping find_people",
            )
            return []
        args: dict[str, Any] = {
            "domain": domain,
            "company_name": company_name or domain,
            "size": limit,
        }
        if titles:
            args["titles"] = titles
            args["title"] = titles[0]
        data = self._call(tool, args)
        if not data:
            return []
        out: list[PersonHit] = []
        for row in unwrap_records(data)[:limit]:
            person = person_from_row(_flatten_person(row), self.tier)
            if not person:
                continue
            phone = person.phone or extract_phone(_flatten_person(row))
            if phone and not person.phone:
                person.phone = phone
            out.append(person)
        if out:
            self.hits += 1
        return out

    def search_people(
        self, filters: dict, *, page: int = 0, size: int = 100
    ) -> dict[str, Any]:
        empty = {"people": [], "total": None, "next_page": None}
        if not self.enabled:
            return empty
        try:
            tools = self.list_tools()
        except Exception as exc:
            bump_errors(self)
            log_vendor_failure(self.tier, settings.getleads_mcp_url, error=str(exc))
            return empty
        tool = pick_tool(tools, score_search_tool)
        if not tool:
            _log_gap_once(
                "_MISSING_SEARCH_LOGGED",
                "getleads has no tools/list entry covering ICP people search; skipping search_people",
            )
            return empty
        args: dict[str, Any] = dict(filters or {})
        args["page"] = page
        args["size"] = size
        data = self._call(tool, args)
        if not data:
            return empty
        people: list[PersonHit] = []
        for row in unwrap_records(data)[:size]:
            person = person_from_row(_flatten_person(row), self.tier)
            if person:
                phone = person.phone or extract_phone(_flatten_person(row))
                if phone and not person.phone:
                    person.phone = phone
                people.append(person)
        if people:
            self.hits += 1
        return {
            "people": people,
            "total": extract_total(data, people),
            "next_page": extract_next_page(data, page, people, size),
        }
