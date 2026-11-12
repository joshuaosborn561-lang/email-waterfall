"""PHONE_LOOKUPS_ENABLED kill switch (v1.7.0, default off).

Off: no phone/mobile vendor endpoint is reached (AI Ark mobile-phone-finder,
Prospeo enrich_mobile, FullEnrich contact.phones, Veriphone). The HTTP layer
is replaced with a trap that records every request and fails the test if a
phone endpoint is hit. Email / people results must match the pre-1.7.0
behavior exactly. On: legacy phone behavior.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import NonCallableMock

import pytest

from email_waterfall import waterfall
from email_waterfall.need import (
    CAP_PHONE,
    PHONE_DISABLED_WARNING,
    NeedViolation,
    assert_capability,
    phone_gate,
    resolve_need,
    using_need,
)
from email_waterfall.vendors.ai_ark import AiArkClient
from email_waterfall.vendors.fullenrich import FullEnrichClient
from email_waterfall.vendors.prospeo import ProspeoClient
from email_waterfall.vendors.veriphone import VeriphoneClient
from tests.test_estimate import _mute_smartlead
from tests.test_waterfall import _patch_writes, _vendor

# Keys ReplyHandler reads off POST /enrich-one / enrich_person.
REPLYHANDLER_KEYS = {
    "ok",
    "email",
    "phone",
    "linkedin_url",
    "website",
    "email_tier",
    "phone_tier",
    "phone_type",
    "sources",
    "fullenrich",
    "status",
}

ROW = {
    "domain": "roofco.com",
    "first_name": "Jane",
    "last_name": "Smith",
    "company_name": "Roof Co",
    "linkedin_url": "https://www.linkedin.com/in/jane-smith",
}


class _Resp:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers: dict[str, str] = {}

    def json(self):
        return self._payload


def _is_phone_request(tier: str, url: str, body: Any) -> bool:
    if "mobile-phone-finder" in url:
        return True
    if tier == "veriphone" or "veriphone" in url:
        return True
    if isinstance(body, dict):
        if tier == "prospeo" and body.get("enrich_mobile"):
            return True
        for item in body.get("data") or []:
            if isinstance(item, dict) and "contact.phones" in (
                item.get("enrich_fields") or []
            ):
                return True
    return False


class HttpTrap:
    """Fake email_waterfall.http_client. Phone endpoints fail the test unless allowed."""

    def __init__(
        self,
        *,
        allow_phone: bool = False,
        aiark_email: str = "jane@roofco.com",
        fullenrich_email: str = "jane@roofco.com",
    ):
        self.allow_phone = allow_phone
        self.aiark_email = aiark_email
        self.fullenrich_email = fullenrich_email
        self.calls: list[tuple[str, str, str]] = []
        self.phone_calls: list[tuple[str, str]] = []
        self.fullenrich_fields: list[list[str]] = []

    def _check(self, tier: str, url: str, body: Any) -> None:
        if _is_phone_request(tier, url, body):
            self.phone_calls.append((tier, url))
            if not self.allow_phone:
                pytest.fail(f"phone endpoint hit while disabled: {tier} {url}")

    def post(self, tier, url, *, json=None, headers=None, timeout=45):
        self._check(tier, url, json)
        self.calls.append(("POST", tier, url.split("?")[0]))
        if tier == "aiark":
            if url.endswith("/v2/people/export/single") and self.aiark_email:
                return _Resp(
                    200,
                    {
                        "status": 200,
                        "data": {
                            "email": {"value": self.aiark_email, "state": "DONE"}
                        },
                    },
                )
            if url.endswith("/v2/people/mobile-phone-finder"):
                return _Resp(200, {"data": {"mobile": "+12015550100"}})
            return _Resp(200, {"content": []})
        if tier == "prospeo":
            if json and json.get("enrich_mobile"):
                return _Resp(200, {"person": {"mobile": "+12015550111"}})
            return _Resp(200, {"error": True, "error_code": "NO_MATCH"})
        if tier == "fullenrich":
            self.fullenrich_fields.append(
                list((json or {}).get("data", [{}])[0].get("enrich_fields") or [])
            )
            return _Resp(200, {"enrichment_id": "enr_1"})
        return _Resp(200, {})

    def get(self, tier, url, *, headers=None, timeout=45):
        self._check(tier, url, None)
        self.calls.append(("GET", tier, url.split("?")[0]))
        if tier == "fullenrich":
            contact: dict[str, Any] = {"custom": {"idx": "0"}}
            if self.fullenrich_email:
                contact["contact_info"] = {
                    "most_probable_work_email": {"email": self.fullenrich_email}
                }
            return _Resp(200, {"status": "FINISHED", "datas": [contact]})
        if tier == "veriphone":
            return _Resp(
                200,
                {
                    "status": "success",
                    "phone_valid": True,
                    "phone_type": "mobile",
                    "e164": "+12015550100",
                    "phone": "+12015550100",
                },
            )
        return _Resp(200, {})


def _install(monkeypatch, trap: HttpTrap, *, getleads=None) -> None:
    monkeypatch.setattr("email_waterfall.http_client.post", trap.post)
    monkeypatch.setattr("email_waterfall.http_client.get", trap.get)
    monkeypatch.setattr(
        "email_waterfall.vendors.fullenrich.time.sleep", lambda *_: None
    )
    monkeypatch.setattr(waterfall, "GetLeadsClient", lambda: getleads or _vendor(email=None))
    monkeypatch.setattr(
        waterfall, "SmartleadClient", lambda *a, **k: _vendor(enabled=False)
    )
    monkeypatch.setattr(waterfall, "AiArkClient", lambda *a, **k: AiArkClient(api_key="ark"))
    monkeypatch.setattr(
        waterfall, "ProspeoClient", lambda *a, **k: ProspeoClient(api_key="pro")
    )
    monkeypatch.setattr(
        waterfall, "FullEnrichClient", lambda *a, **k: FullEnrichClient(api_key="fe")
    )
    monkeypatch.setattr(
        waterfall, "VeriphoneClient", lambda *a, **k: VeriphoneClient(api_key="vp")
    )


def _no_ensure_client(monkeypatch) -> list[str]:
    seen: list[str] = []

    def boom(tag, **_k):
        seen.append(tag)
        raise AssertionError("phone-only call must return before ensure_client")

    monkeypatch.setattr(waterfall, "ensure_client", boom)
    return seen


# --------------------------------------------------------------------------
# Gate unit behavior
# --------------------------------------------------------------------------


def test_default_is_off_and_need_both_maps_to_people_email() -> None:
    from email_waterfall import config

    assert config.Settings.__dataclass_fields__["phone_lookups_enabled"].default is False
    assert resolve_need("both") == ("people_email", frozenset({"people", "email"}))
    assert resolve_need("email")[0] == "email"
    gate = phone_gate("both")
    assert gate.disabled and not gate.phone_only
    assert phone_gate("phone").phone_only
    assert phone_gate(None, find_phone=True).phone_only
    assert phone_gate(None, find_phone=True, find_email=True).need == "email"
    assert phone_gate("email", verify_only=True).phone_only
    assert not phone_gate("email").disabled


def test_assert_capability_phone_always_raises_when_off() -> None:
    with using_need("phone", frozenset({"phone", "people"})):
        with pytest.raises(NeedViolation, match="phone lookups disabled"):
            assert_capability(CAP_PHONE, vendor="aiark", endpoint="mobile")


@pytest.mark.usefixtures("phone_lookups_on")
def test_gate_is_transparent_when_on() -> None:
    assert resolve_need("both") == (
        "both",
        frozenset({"people", "email", "phone"}),
    )
    gate = phone_gate("phone", verify_only=True)
    assert not gate.disabled and not gate.phone_only
    with using_need("phone"):
        assert_capability(CAP_PHONE, vendor="aiark", endpoint="mobile")


# --------------------------------------------------------------------------
# Bulk enrich_waterfall
# --------------------------------------------------------------------------


def test_bulk_need_both_off_makes_no_phone_http_and_keeps_email(monkeypatch) -> None:
    trap = HttpTrap()
    _install(monkeypatch, trap)
    sink: dict = {}
    _patch_writes(monkeypatch, sink)

    out = waterfall.enrich_waterfall(
        [dict(ROW)],
        client_tag="peterson",
        need="both",
        max_tier="prospeo",
        write_supabase=True,
        parallel=False,
    )
    assert trap.phone_calls == []
    assert out["emails_found"] == 1
    assert out["phones_found"] == 0
    assert out["need"] == "people_email"
    assert out["need_requested"] == "both"
    assert "phone" not in out["need_capabilities"]
    assert PHONE_DISABLED_WARNING in out["warnings"]
    assert out["phone_lookups"] == {
        "enabled": False,
        "status": "disabled",
        "message": PHONE_DISABLED_WARNING,
    }
    # Suppressed counts reuse the suppressed_by_need style.
    assert out["suppressed_by_need"].get("aiark_phone") == 1
    assert sink["contacts"][0]["email"] == "jane@roofco.com"
    assert not sink["contacts"][0].get("cellphone")


def _norm(value: Any) -> Any:
    """Drop test-double noise (MagicMock reprs carry object ids)."""
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if isinstance(value, NonCallableMock) or (
        isinstance(value, str) and "<MagicMock" in value
    ):
        return "<mock>"
    return value


def _no_ts(contacts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        {k: v for k, v in c.items() if k != "updated_at"} for c in contacts or []
    ]


def test_bulk_need_email_identical_off_vs_on(monkeypatch) -> None:
    from tests.conftest import _set_phone_lookups

    results = []
    calls = []
    getleads = _vendor(email=None)  # shared so mock reprs match across runs
    for flag in ("false", "true"):
        _set_phone_lookups(monkeypatch, flag)
        trap = HttpTrap()
        _install(monkeypatch, trap, getleads=getleads)
        sink: dict = {}
        _patch_writes(monkeypatch, sink)
        out = waterfall.enrich_waterfall(
            [dict(ROW)],
            client_tag="peterson",
            need="email",
            write_supabase=True,
            parallel=False,
        )
        results.append((_norm(out), _no_ts(sink.get("contacts"))))
        calls.append(trap.calls)
        assert trap.phone_calls == []
    assert results[0] == results[1]
    assert calls[0] == calls[1]


def test_bulk_need_both_off_matches_people_email_on(monkeypatch) -> None:
    """need='both' with the switch off makes the same vendor calls and finds
    the same emails as need='people_email' did before 1.7.0."""
    from tests.conftest import _set_phone_lookups

    runs = {}
    getleads = _vendor(email=None)
    for flag, need in (("false", "both"), ("true", "people_email")):
        _set_phone_lookups(monkeypatch, flag)
        trap = HttpTrap(aiark_email="")  # AI Ark misses; Prospeo misses too
        _install(monkeypatch, trap, getleads=getleads)
        sink: dict = {}
        _patch_writes(monkeypatch, sink)
        out = waterfall.enrich_waterfall(
            [dict(ROW)],
            client_tag="peterson",
            need=need,
            write_supabase=True,
            parallel=False,
        )
        runs[flag] = (_norm(out), _no_ts(sink.get("contacts")), trap.calls)
    off, on = runs["false"], runs["true"]
    assert off[2] == on[2]
    assert off[1] == on[1]
    for key in ("emails_found", "dms_found", "phones_found", "tier_breakdown",
                "suppressed_by_need", "need_capabilities", "spend"):
        assert off[0][key] == on[0][key], key


@pytest.mark.parametrize(
    "kwargs",
    [
        {"need": "phone"},
        {"find_phone": True},
        {"find_phone": True, "find_people": True},
        {"need": "email", "verify_only": True},
        {"need": "phone", "estimate_only": True},
    ],
)
def test_bulk_phone_only_returns_fast_without_paid_work(monkeypatch, kwargs) -> None:
    trap = HttpTrap()
    _install(monkeypatch, trap)
    seen = _no_ensure_client(monkeypatch)
    out = waterfall.enrich_waterfall(
        [dict(ROW, phone="+12015550100"), {"domain": "acme.com"}],
        client_tag="peterson",
        write_supabase=True,
        **kwargs,
    )
    assert trap.calls == []
    assert seen == []
    assert out["status"] == "phone_lookups_disabled"
    assert out["warnings"] == [PHONE_DISABLED_WARNING]
    assert out["spend"] == 0
    assert out["estimated_cost_usd"] == 0.0
    assert out["rows_in"] == 2
    assert out["suppressed_by_need"] == {"phone_disabled_rows": 2}
    assert out["phone_lookups"]["enabled"] is False


def test_estimate_off_excludes_phone_costs(monkeypatch) -> None:
    _mute_smartlead(monkeypatch)
    out = waterfall.enrich_waterfall(
        [dict(ROW)],
        client_tag="peterson",
        need="both",
        max_tier="prospeo",
        estimate_only=True,
        write_supabase=False,
    )
    assert out["estimate"]["aiark"]["credits_per_row"] == 1.5
    assert out["estimate"]["aiark"]["rate_note"] == "email-only"
    assert out["estimate"]["prospeo"]["credits_per_row"] == 1.0
    assert out["need_capabilities"] == ["email", "people"]
    assert PHONE_DISABLED_WARNING in out["warnings"]
    assert out["suppressed_by_need"].get("aiark_phone") == 1


@pytest.mark.usefixtures("phone_lookups_on")
def test_estimate_on_still_includes_phone_costs(monkeypatch) -> None:
    _mute_smartlead(monkeypatch)
    out = waterfall.enrich_waterfall(
        [dict(ROW)],
        client_tag="peterson",
        need="both",
        max_tier="prospeo",
        estimate_only=True,
        write_supabase=False,
    )
    assert out["estimate"]["aiark"]["credits_per_row"] == 6.5
    assert out["estimate"]["prospeo"]["credits_per_row"] == 11.0
    assert "phone_lookups" not in out
    assert PHONE_DISABLED_WARNING not in out["warnings"]


@pytest.mark.usefixtures("phone_lookups_on")
def test_bulk_need_both_on_calls_phone_endpoints(monkeypatch) -> None:
    trap = HttpTrap(allow_phone=True)
    _install(monkeypatch, trap)
    sink: dict = {}
    _patch_writes(monkeypatch, sink)
    out = waterfall.enrich_waterfall(
        [dict(ROW)],
        client_tag="peterson",
        need="both",
        write_supabase=True,
        parallel=False,
    )
    assert any("mobile-phone-finder" in url for _t, url in trap.phone_calls)
    assert out["need"] == "both"
    assert out["phones_found"] == 1
    assert "phone_lookups" not in out
    assert "need_requested" not in out


# --------------------------------------------------------------------------
# ReplyHandler: enrich_person / POST /enrich-one
# --------------------------------------------------------------------------


def test_enrich_one_off_keeps_fullenrich_email_without_phones(monkeypatch) -> None:
    trap = HttpTrap(aiark_email="")  # AI Ark + Prospeo miss -> FullEnrich email
    _install(monkeypatch, trap)
    hit = waterfall.enrich_one_person(
        client_tag="replyhandler",
        first_name="Jane",
        last_name="Smith",
        domain="roofco.com",
        linkedin_url=ROW["linkedin_url"],
    )
    assert trap.phone_calls == []
    assert trap.fullenrich_fields == [["contact.work_emails"]]
    assert REPLYHANDLER_KEYS <= set(hit)
    assert hit["ok"] is True
    assert hit["email"] == "jane@roofco.com"
    assert hit["email_tier"] == "fullenrich"
    assert hit["linkedin_url"] == ROW["linkedin_url"]
    assert hit["website"] == "https://roofco.com"
    assert hit["phone"] == ""
    assert hit["phone_tier"] == ""
    assert hit["phone_type"] == ""
    assert hit["sources"]["phone"] is None
    assert hit["sources"]["email"] == "fullenrich"
    assert hit["phone_status"] == "disabled"
    assert hit["fullenrich"]["mobile"] == "not_run"
    assert hit["fullenrich"]["mobile_reason"] == "disabled"
    assert set(hit["fullenrich"]) >= {"enabled", "mobile", "credits_exhausted"}
    assert hit["need"] == "people_email"
    assert hit["need_requested"] == "both"
    assert PHONE_DISABLED_WARNING in hit["warnings"]


def test_enrich_one_need_phone_off_returns_fast(monkeypatch) -> None:
    trap = HttpTrap()
    _install(monkeypatch, trap)
    seen = _no_ensure_client(monkeypatch)
    hit = waterfall.enrich_one_person(
        client_tag="replyhandler",
        email="Jane@RoofCo.com",
        domain="roofco.com",
        need="phone",
    )
    assert trap.calls == []
    assert seen == []
    assert REPLYHANDLER_KEYS <= set(hit)
    assert hit["status"] == "phone_lookups_disabled"
    assert hit["email"] == "jane@roofco.com"
    assert hit["phone"] == ""
    assert hit["phone_status"] == "disabled"
    assert hit["fullenrich"]["mobile"] == "not_run"
    assert hit["spend"] == 0
    assert hit["warnings"] == [PHONE_DISABLED_WARNING]


@pytest.mark.usefixtures("phone_lookups_on")
def test_enrich_one_on_still_finds_phone(monkeypatch) -> None:
    trap = HttpTrap(allow_phone=True)
    _install(monkeypatch, trap)
    hit = waterfall.enrich_one_person(
        client_tag="replyhandler",
        first_name="Jane",
        last_name="Smith",
        domain="roofco.com",
        linkedin_url=ROW["linkedin_url"],
    )
    assert trap.phone_calls
    assert hit["phone"] == "+12015550100"
    assert hit["need"] == "both"
    assert "phone_status" not in hit
    assert "mobile_reason" not in hit["fullenrich"]


def test_enrich_person_tool_off(monkeypatch) -> None:
    from mcp_server import server as srv

    trap = HttpTrap(aiark_email="")
    _install(monkeypatch, trap)
    hit = json.loads(
        srv.enrich_person(
            client_tag="replyhandler",
            first_name="Jane",
            last_name="Smith",
            domain="roofco.com",
        )
    )
    assert trap.phone_calls == []
    assert hit["email"] == "jane@roofco.com"
    assert hit["phone_status"] == "disabled"


def test_enrich_waterfall_tool_need_phone_off_skips_ensure_client(monkeypatch) -> None:
    from email_waterfall import clients
    from mcp_server import server as srv

    trap = HttpTrap()
    _install(monkeypatch, trap)

    def boom(*_a, **_k):
        raise AssertionError("ensure_client must not run for need=phone")

    monkeypatch.setattr(clients, "ensure_client", boom)
    out = json.loads(
        srv.enrich_waterfall(
            client_tag="peterson",
            rows=[dict(ROW)],
            need="phone",
            background=False,
        )
    )
    assert trap.calls == []
    assert out["status"] == "phone_lookups_disabled"
    assert PHONE_DISABLED_WARNING in out["warnings"]


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------


def test_health_reports_phone_lookups_disabled() -> None:
    from mcp_server.server import health

    data = json.loads(health())
    assert data["phone_lookups_enabled"] is False
    note = data["deprecations"]["phone_lookups"]
    assert "phone-waterfall MCP" in note
    assert "PHONE_LOOKUPS_ENABLED=false" in note


@pytest.mark.usefixtures("phone_lookups_on")
def test_health_reports_phone_lookups_enabled() -> None:
    from mcp_server.server import health

    data = json.loads(health())
    assert data["phone_lookups_enabled"] is True
    assert "phone-waterfall MCP" in data["deprecations"]["phone_lookups"]
