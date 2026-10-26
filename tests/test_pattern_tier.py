"""Pattern tier: one known-format guess, MillionVerifier `ok` only."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from email_waterfall.need import set_need, reset_need


@pytest.fixture(autouse=True)
def _isolate_need():
    """resolve_email binds `need` on this thread; restore it after each test."""
    token = set_need("both")
    yield
    reset_need(token)

from email_waterfall import waterfall
from email_waterfall._wf.const import (
    CREDIT_PER_ATTEMPT,
    USD_PER_CREDIT,
    normalize_max_tier,
    resolve_skip_tiers,
    tier_allowed,
)
from email_waterfall.names import norm_first, norm_last
from email_waterfall.vendors import pattern as pattern_mod
from email_waterfall.vendors.base import EmailHit
from email_waterfall.vendors.pattern import PatternGuessClient, build


def _vendor(*, enabled=True, email=None):
    m = MagicMock()
    m.enabled = enabled
    m.calls = 0
    m.hits = 0
    m.errors = 0
    m.find_people.return_value = []
    m.find_email.return_value = email
    m.find_linkedin.return_value = ""
    m.find_mobile.return_value = None
    return m


class _Resp:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status
        self.text = json.dumps(body)

    def json(self):
        return self._body


@pytest.fixture
def supa(monkeypatch):
    """Fake Supabase: domain_email_patterns rows + recorded RPCs."""
    state = {"rows": {}, "rpc": [], "gets": 0}
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")

    def fake_request(method, path, body=None, prefer="return=minimal"):
        state["gets"] += 1
        dom = path.split("domain=eq.", 1)[1].split("&", 1)[0]
        return 200, json.dumps(state["rows"].get(dom, []))

    def fake_rpc(name, body=None):
        state["rpc"].append((name, body))
        return None

    monkeypatch.setattr(waterfall.supabase_sync, "_request", fake_request)
    monkeypatch.setattr(waterfall.supabase_sync, "rpc", fake_rpc)
    return state


@pytest.fixture
def mv(monkeypatch):
    calls = []
    result = {"value": "ok"}

    def fake_get(tier, url, headers=None, timeout=45):
        assert tier == "pattern"
        calls.append(url)
        return _Resp({"result": result["value"], "email": url.split("email=")[1].split("&")[0]})

    monkeypatch.setattr("email_waterfall.http_client.get", fake_get)
    return {"calls": calls, "result": result}


def _wf(pattern, prospeo, **kw):
    return waterfall.Waterfall(
        getleads=_vendor(enabled=False),
        smartlead=_vendor(enabled=False),
        ai_ark=_vendor(enabled=False),
        prospeo=prospeo,
        fullenrich=_vendor(enabled=False),
        veriphone=_vendor(enabled=False),
        blitz=_vendor(enabled=False),
        serper=_vendor(enabled=False),
        pattern=pattern,
        need="email",
        **kw,
    )


def _row(first="Jane", last="Smith", domain="roofco.com"):
    return {
        "first_name": first, "last_name": last, "domain": domain,
        "company_name": "", "linkedin_url": "", "phone": "", "email": "",
        "full_name": f"{first} {last}", "title": "",
    }


# -- names + build ---------------------------------------------------------

def test_name_normalization() -> None:
    assert norm_first("R. Todd") == "todd"
    assert norm_last("Smith MD") == "smith"
    assert norm_last("Smith, Jr.") == "smith"
    assert norm_first("Dr. José") == "jose"
    assert norm_last("de la Cruz") == "cruz"
    assert norm_last("O'Brien III") == "obrien"
    assert norm_first("Mary Ann") == "mary"


@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("flast", "jsmith"),
        ("first.last", "jane.smith"),
        ("firstlast", "janesmith"),
        ("firstl", "janes"),
        ("first", "jane"),
        ("first_last", "jane_smith"),
        ("f.last", "j.smith"),
        ("lastf", "smithj"),
        ("last.first", "smith.jane"),
        ("first-last", "jane-smith"),
    ],
)
def test_build_every_pattern(pattern, expected) -> None:
    assert build("Jane", "Smith", pattern, "roofco.com") == f"{expected}@roofco.com"


def test_build_suffix_initial_and_short() -> None:
    assert build("R. Todd", "Smith MD", "first.last", "x.com") == "todd.smith@x.com"
    assert build("J", "Smith", "flast", "x.com") is None
    assert build("Jane", "X", "flast", "x.com") is None
    assert build("Jane", "Smith", "nonsense", "x.com") is None


# -- const --------------------------------------------------------------------

def test_tier_controls_accept_pattern() -> None:
    assert normalize_max_tier("pattern") == "pattern"
    assert normalize_max_tier("mv_guess") == "pattern"
    assert normalize_max_tier("guess") == "pattern"
    assert normalize_max_tier("pattern_guess") == "pattern"
    assert resolve_skip_tiers("pattern")[0] == {"pattern"}
    assert not tier_allowed("pattern", "aiark")
    assert tier_allowed("pattern", "prospeo")
    assert "pattern" not in waterfall.NAME_COMPANY_TIERS
    assert CREDIT_PER_ATTEMPT["pattern"] == 1.0
    assert USD_PER_CREDIT["pattern"] == pytest.approx(0.0037)


# -- client -------------------------------------------------------------------

def test_no_known_pattern_no_mv_call_goes_to_prospeo(supa, mv) -> None:
    prospeo = _vendor(email=EmailHit(email="jane@roofco.com", source_tier="prospeo"))
    pc = PatternGuessClient(api_key="mv")
    wf = _wf(pc, prospeo)
    hit = wf.resolve_email(_row())
    assert mv["calls"] == []
    assert hit.source_tier == "prospeo"
    assert wf.tier_stats["pattern"]["calls"] == 0
    assert "credits_charged" not in wf.tier_stats["pattern"]


def test_catch_all_domain_no_call(supa, mv) -> None:
    supa["rows"]["roofco.com"] = [
        {"pattern": "first.last", "email_domain": "roofco.com", "n_verified": 5, "is_catch_all": True}
    ]
    pc = PatternGuessClient(api_key="mv")
    assert pc.find_email("Jane", "Smith", "roofco.com") is None
    assert mv["calls"] == []


def test_mv_ok_hits_and_prospeo_never_called(supa, mv) -> None:
    supa["rows"]["roofco.com"] = [
        {"pattern": "flast", "email_domain": "roof.co", "n_verified": 3, "is_catch_all": False}
    ]
    prospeo = _vendor(email=EmailHit(email="x@roofco.com", source_tier="prospeo"))
    pc = PatternGuessClient(api_key="mv")
    wf = _wf(pc, prospeo)
    hit = wf.resolve_email(_row())
    assert hit.source_tier == "pattern"
    assert hit.email == "jsmith@roof.co"
    assert hit.status == "ok"
    assert hit.raw["pattern"] == "flast"
    assert len(mv["calls"]) == 1
    assert "timeout=10" in mv["calls"][0]
    prospeo.find_email.assert_not_called()
    assert wf.tier_stats["pattern"]["email_hits"] == 1
    assert pc.credits_charged == 1.0
    assert ("ew_bump_domain_pattern", {"p_domain": "roofco.com", "p_email_domain": "roof.co", "p_pattern": "flast"}) in supa["rpc"]


def test_catch_all_result_marks_domain_and_falls_through(supa, mv) -> None:
    supa["rows"]["roofco.com"] = [
        {"pattern": "first.last", "email_domain": "roofco.com", "n_verified": 2, "is_catch_all": False}
    ]
    mv["result"]["value"] = "catch_all"
    prospeo = _vendor(email=EmailHit(email="jane@roofco.com", source_tier="prospeo"))
    pc = PatternGuessClient(api_key="mv")
    wf = _wf(pc, prospeo)
    hit = wf.resolve_email(_row())
    assert hit.source_tier == "prospeo"
    assert any(name == "ew_mark_domain_catch_all" for name, _ in supa["rpc"])
    # Marked: a second person at the domain is never guessed.
    assert pc.find_email("Bob", "Jones", "roofco.com") is None
    assert len(mv["calls"]) == 1


@pytest.mark.parametrize("result", ["unknown", "invalid", "disposable", "error"])
def test_non_ok_results_are_misses(supa, mv, result) -> None:
    supa["rows"]["roofco.com"] = [
        {"pattern": "first.last", "email_domain": "roofco.com", "n_verified": 2, "is_catch_all": False}
    ]
    mv["result"]["value"] = result
    pc = PatternGuessClient(api_key="mv")
    assert pc.find_email("Jane", "Smith", "roofco.com") is None


def test_lookup_cached_per_job(supa, mv) -> None:
    supa["rows"]["roofco.com"] = [
        {"pattern": "first.last", "email_domain": "roofco.com", "n_verified": 2, "is_catch_all": False}
    ]
    pc = PatternGuessClient(api_key="mv")
    pc.find_email("Jane", "Smith", "roofco.com")
    pc.find_email("Bob", "Jones", "roofco.com")
    assert supa["gets"] == 1
    assert len(mv["calls"]) == 2


def test_max_tier_aiark_and_skip_pattern(supa, mv) -> None:
    supa["rows"]["roofco.com"] = [
        {"pattern": "first.last", "email_domain": "roofco.com", "n_verified": 2, "is_catch_all": False}
    ]
    pc = PatternGuessClient(api_key="mv")
    wf = _wf(pc, _vendor(enabled=False), max_tier="aiark")
    assert wf.resolve_email(_row()) is None
    wf2 = _wf(PatternGuessClient(api_key="mv"), _vendor(enabled=False), skip_tiers={"pattern"})
    assert wf2.resolve_email(_row()) is None
    assert mv["calls"] == []


def test_attempts_count_against_ceiling(supa, mv) -> None:
    supa["rows"]["roofco.com"] = [
        {"pattern": "first.last", "email_domain": "roofco.com", "n_verified": 2, "is_catch_all": False}
    ]
    mv["result"]["value"] = "invalid"
    pc = PatternGuessClient(api_key="mv")
    wf = _wf(pc, _vendor(enabled=False), approve_cost_usd=0.008)
    rows = [_row(first=f) for f in ("Jane", "Bob", "Ann")]
    for row in rows:
        wf.resolve_email(row)
    # 0.0037 each: two fit under $0.008, the third stops at the ceiling.
    assert len(mv["calls"]) == 2
    assert wf.spend_usd == pytest.approx(0.0074)
    assert wf.stopped_at_ceiling is True


def test_disabled_without_key_or_supabase(monkeypatch) -> None:
    monkeypatch.delenv("MILLIONVERIFIER_API_KEY", raising=False)
    assert PatternGuessClient().enabled is False


def test_estimate_includes_pattern_cost(monkeypatch) -> None:
    monkeypatch.setattr(waterfall, "SmartleadClient", lambda *a, **k: _vendor(enabled=False))
    out = waterfall.enrich_waterfall(
        [{"domain": "roofco.com", "first_name": "Jane", "last_name": "Smith"}],
        client_tag="peterson",
        need="email",
        estimate_only=True,
        write_supabase=False,
    )
    assert out["estimate"]["pattern"]["usd_est"] == pytest.approx(0.0037)
    assert "pattern" in out["tiers_by_mode"]["domain"]
    out2 = waterfall.enrich_waterfall(
        [{"domain": "roofco.com", "first_name": "Jane", "last_name": "Smith"}],
        client_tag="peterson",
        need="email",
        max_tier="aiark",
        estimate_only=True,
        write_supabase=False,
    )
    assert "pattern" not in out2["estimate"]
