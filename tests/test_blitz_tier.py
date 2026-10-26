"""Blitz tier for IT rows + LinkedIn resolver + FullEnrich bulk exclusion."""

from __future__ import annotations

import json
import time
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
from email_waterfall.it_lane import is_it_row, it_row_reason
from email_waterfall.vendors import blitz as blitz_mod
from email_waterfall.vendors.base import EmailHit
from email_waterfall.vendors.blitz import BlitzClient


def _vendor(*, enabled=True, email=None):
    m = MagicMock()
    m.enabled = enabled
    m.calls = 0
    m.hits = 0
    m.errors = 0
    m.find_people.return_value = []
    m.find_email.return_value = email
    m.find_email_bulk.return_value = []
    m.find_linkedin.return_value = ""
    m.find_mobile.return_value = None
    m.lookup_pattern.return_value = None
    return m


def _blitz_mock(*, email=None, linkedin="", records=50000):
    m = _vendor(email=email)
    m.out_of_records = False
    m.records_remaining = records
    m.key_info.return_value = {"records_remaining": records}
    m.find_linkedin.return_value = linkedin
    return m


def _wf(*, gl=None, blitz=None, serper=None, prospeo=None, fe=None, **kw):
    return waterfall.Waterfall(
        getleads=gl or _vendor(enabled=False),
        smartlead=_vendor(enabled=False),
        ai_ark=_vendor(enabled=False),
        prospeo=prospeo or _vendor(enabled=False),
        fullenrich=fe or _vendor(enabled=False),
        veriphone=_vendor(enabled=False),
        pattern=_vendor(enabled=False),
        blitz=blitz or _blitz_mock(),
        serper=serper or _vendor(enabled=False),
        need="email",
        **kw,
    )


def _row(title="IT Director", linkedin="", first="Jane", last="Smith"):
    return {
        "first_name": first, "last_name": last, "domain": "roofco.com",
        "company_name": "Roof Co", "linkedin_url": linkedin, "phone": "",
        "email": "", "full_name": f"{first} {last}", "title": title,
    }


# -- is_it_row ------------------------------------------------------------------

def test_is_it_row_reasons() -> None:
    assert it_row_reason({"title": "Owner"}, None, True) == "param"
    assert it_row_reason({"title": "Owner"}, {"it_lane": True}, None) == "profile"
    assert it_row_reason({"title": "Director of IT"}, None, None) == "title"
    for t in ("CIO", "CTO", "CISO", "IT Manager", "VP of IT", "Network Engineer",
              "Systems Administrator", "Help Desk Lead", "Director of Technology"):
        assert is_it_row({"title": t}, None, None), t


def test_is_it_row_false_positives() -> None:
    for t in ("CNO", "Chief Nursing Officer", "VP Sales", "Network Sales Manager",
              "HR Systems Manager", "Technical Recruiter", "Owner", "Make it happen"):
        assert not is_it_row({"title": t}, None, None), t
    assert not is_it_row({"title": "VP Sales"}, {"it_lane": False}, False)


# -- engine ----------------------------------------------------------------------

def test_it_row_with_linkedin_blitz_first_hit_skips_getleads() -> None:
    gl = _vendor(email=EmailHit(email="x@roofco.com", source_tier="getleads"))
    bz = _blitz_mock(email=EmailHit(email="jane@roofco.com", source_tier="blitz"))
    wf = _wf(gl=gl, blitz=bz)
    hit = wf.resolve_email(_row(linkedin="https://www.linkedin.com/in/jane"))
    assert hit.source_tier == "blitz"
    bz.find_email.assert_called_once_with("https://www.linkedin.com/in/jane")
    gl.find_email.assert_not_called()
    assert wf.tier_stats["blitz"]["email_hits"] == 1
    assert wf.blitz_stats["it_rows_by_reason"] == {"title": 1}


def test_non_it_row_never_calls_blitz() -> None:
    gl = _vendor(email=None)
    bz = _blitz_mock(email=EmailHit(email="jane@roofco.com", source_tier="blitz"))
    wf = _wf(gl=gl, blitz=bz)
    wf.resolve_email(_row(title="VP Sales", linkedin="https://www.linkedin.com/in/jane"))
    bz.find_email.assert_not_called()
    bz.key_info.assert_not_called()
    gl.find_email.assert_called_once()


def test_skip_tiers_blitz() -> None:
    bz = _blitz_mock(email=EmailHit(email="jane@roofco.com", source_tier="blitz"))
    wf = _wf(blitz=bz, skip_tiers={"blitz"})
    wf.resolve_email(_row(linkedin="https://www.linkedin.com/in/jane"))
    bz.find_email.assert_not_called()


def test_records_guard_refuses_low_balance(monkeypatch) -> None:
    monkeypatch.setenv("BLITZ_MIN_RECORDS_LEFT", "2000")
    bz = _blitz_mock(email=EmailHit(email="j@roofco.com", source_tier="blitz"), records=1500)
    wf = _wf(blitz=bz)
    wf.resolve_email(_row(linkedin="https://www.linkedin.com/in/jane"))
    bz.find_email.assert_not_called()
    assert "blitz" in wf._circuit_disabled


def test_resolver_order_getleads_hit_then_blitz_email() -> None:
    gl = _vendor(email=None)
    gl.find_linkedin.return_value = "https://www.linkedin.com/in/jane-gl"
    bz = _blitz_mock(email=EmailHit(email="jane@roofco.com", source_tier="blitz"))
    serper = _vendor()
    wf = _wf(gl=gl, blitz=bz, serper=serper)
    row = _row()
    hit = wf.resolve_email(row)
    assert hit.source_tier == "blitz"
    assert row["linkedin_url"] == "https://www.linkedin.com/in/jane-gl"
    bz.find_linkedin.assert_not_called()
    serper.find_linkedin.assert_not_called()
    bz.find_email.assert_called_once_with("https://www.linkedin.com/in/jane-gl")
    assert wf.blitz_stats["linkedin_found_by"] == {"getleads": 1}


def test_resolver_falls_to_blitz_then_serper() -> None:
    gl = _vendor(email=None)
    bz = _blitz_mock(email=None, linkedin="")
    serper = _vendor()
    serper.find_linkedin.return_value = "https://www.linkedin.com/in/jane-s"
    wf = _wf(gl=gl, blitz=bz, serper=serper)
    row = _row()
    wf.resolve_email(row)
    gl.find_linkedin.assert_called_once()
    bz.find_linkedin.assert_called_once()
    serper.find_linkedin.assert_called_once()
    assert row["linkedin_url"] == "https://www.linkedin.com/in/jane-s"
    bz.find_email.assert_called_once_with("https://www.linkedin.com/in/jane-s")
    assert wf.blitz_stats["linkedin_found_by"] == {"serper": 1}
    # Serper is booked against approve_cost_usd.
    assert wf.spend_usd == pytest.approx(0.001)


def test_resolver_all_miss_continues_at_getleads_without_blitz_email() -> None:
    gl = _vendor(email=EmailHit(email="jane@roofco.com", source_tier="getleads"))
    bz = _blitz_mock()
    serper = _vendor()
    wf = _wf(gl=gl, blitz=bz, serper=serper)
    row = _row()
    hit = wf.resolve_email(row)
    assert hit.source_tier == "getleads"
    bz.find_email.assert_not_called()
    assert row["linkedin_url"] == ""
    assert wf.blitz_stats["linkedin_lookup_misses"] == 1


def test_resolver_step_timeout_never_stalls_row(monkeypatch) -> None:
    monkeypatch.setenv("LINKEDIN_STEP_TIMEOUT_S", "0.2")
    gl = _vendor(email=EmailHit(email="jane@roofco.com", source_tier="getleads"))
    gl.find_linkedin.side_effect = lambda *a: time.sleep(3) or "https://www.linkedin.com/in/late"
    bz = _blitz_mock()
    bz.find_linkedin.side_effect = lambda *a: time.sleep(3) or ""
    serper = _vendor()
    serper.find_linkedin.side_effect = RuntimeError("boom")
    wf = _wf(gl=gl, blitz=bz, serper=serper)
    start = time.monotonic()
    hit = wf.resolve_email(_row())
    assert time.monotonic() - start < 2.0
    assert hit.source_tier == "getleads"
    bz.find_email.assert_not_called()


# -- BlitzClient HTTP ------------------------------------------------------------

class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def _patch_http(monkeypatch, handler):
    calls = []

    def fake(method, url, json=None, headers=None, timeout=None):
        assert headers["x-api-key"] == "bk"
        calls.append((method, url.replace(blitz_mod.BASE_URL, ""), json))
        return handler(method, url.replace(blitz_mod.BASE_URL, ""), json)

    monkeypatch.setattr(blitz_mod.requests, "request", fake)
    return calls


def test_email_shapes(monkeypatch) -> None:
    bodies = iter([
        {"found": True, "email": "A@x.com"},
        {"data": {"work_email": "b@x.com"}},
        {"emails": ["c@x.com"]},
        {"found": False, "email": None},
    ])
    _patch_http(monkeypatch, lambda *a: _Resp(200, next(bodies)))
    c = BlitzClient(api_key="bk", sleep=lambda s: None)
    assert c.find_email("https://linkedin.com/in/a").email == "a@x.com"
    assert c.find_email("https://linkedin.com/in/b").email == "b@x.com"
    assert c.find_email("https://linkedin.com/in/c").email == "c@x.com"
    assert c.find_email("https://linkedin.com/in/d") is None


def test_429_retries_with_backoff(monkeypatch) -> None:
    seq = iter([_Resp(429, {}), _Resp(503, {}), _Resp(200, {"found": True, "email": "a@x.com"})])
    _patch_http(monkeypatch, lambda *a: next(seq))
    slept = []
    c = BlitzClient(api_key="bk", sleep=slept.append)
    hit = c.find_email("https://linkedin.com/in/a")
    assert hit and hit.source_tier == "blitz"
    assert slept == [1.5, 4.0]


def test_402_disables_tier_for_job(monkeypatch) -> None:
    calls = _patch_http(monkeypatch, lambda m, p, j: _Resp(200, {"records_remaining": 90000}) if p.endswith("key-info") else _Resp(402, {"message": "out"}))
    c = BlitzClient(api_key="bk", sleep=lambda s: None)
    wf = _wf(blitz=c, gl=_vendor(email=None))
    rows = [_row(linkedin=f"https://www.linkedin.com/in/p{i}") for i in range(3)]
    for r in rows:
        wf.resolve_email(r)
    email_calls = [c_ for c_ in calls if c_[1] == "/v2/enrichment/email"]
    assert len(email_calls) == 1
    assert "blitz" in wf._circuit_disabled
    assert "blitz: out of records" in wf.control_warnings


def test_company_and_employee_lists_cached(monkeypatch) -> None:
    def handler(method, path, body):
        if path == "/v2/enrichment/domain-to-linkedin":
            return _Resp(200, {"found": True, "company_linkedin_url": "https://www.linkedin.com/company/roofco"})
        if path == "/v2/search/employee-finder":
            assert body["job_function"] == ["Information Technology"]
            assert body["max_results"] == 50
            return _Resp(200, {"results": [
                {"first_name": "Jane", "last_name": "Smith", "linkedin_url": "https://www.linkedin.com/in/jane"},
                {"first_name": "Robert", "last_name": "Jones", "linkedin_url": "https://www.linkedin.com/in/bob"},
                {"first_name": "Zed", "last_name": "Brown", "linkedin_url": "https://www.linkedin.com/in/zed"},
            ]})
        raise AssertionError(path)

    calls = _patch_http(monkeypatch, handler)
    c = BlitzClient(api_key="bk", sleep=lambda s: None)
    assert c.find_linkedin("Jane", "Smith", "roofco.com") == "https://www.linkedin.com/in/jane"
    assert c.find_linkedin("Bob", "Jones", "roofco.com") == "https://www.linkedin.com/in/bob"  # nickname
    # Last name only is rejected.
    assert c.find_linkedin("Alice", "Brown", "roofco.com") == ""
    paths = [p for _, p, _ in calls]
    assert paths.count("/v2/enrichment/domain-to-linkedin") == 1
    assert paths.count("/v2/search/employee-finder") == 1


def test_company_miss_cached(monkeypatch) -> None:
    calls = _patch_http(monkeypatch, lambda *a: _Resp(200, {"found": False}))
    c = BlitzClient(api_key="bk", sleep=lambda s: None)
    assert c.find_linkedin("Jane", "Smith", "nowhere.com") == ""
    assert c.find_linkedin("Bob", "Jones", "nowhere.com") == ""
    assert len(calls) == 1


def test_serper_requires_full_name_and_company(monkeypatch) -> None:
    from email_waterfall.vendors.serper import SerperClient

    results = {"organic": [{"link": "https://www.linkedin.com/in/smith", "title": "Bob Smith - Roof Co | LinkedIn", "snippet": ""}]}

    class R:
        status_code = 200
        def json(self):
            return results

    monkeypatch.setattr("email_waterfall.http_client.post", lambda *a, **k: R())
    s = SerperClient(api_key="sk")
    assert s.find_linkedin("Jane", "Smith", "Roof Co") == ""  # last name only
    results["organic"][0]["title"] = "Jane Smith - Roof Co | LinkedIn"
    assert s.find_linkedin("Jane", "Smith", "Roof Co") == "https://www.linkedin.com/in/smith"
    assert s.calls == 2


# -- Part 0: FullEnrich is ReplyHandler only -----------------------------------

def _patch_hosts(monkeypatch, fe, prospeo):
    monkeypatch.setattr(waterfall, "GetLeadsClient", lambda: _vendor(email=None))
    monkeypatch.setattr(waterfall, "SmartleadClient", lambda *a, **k: _vendor(enabled=False))
    monkeypatch.setattr(waterfall, "AiArkClient", lambda: _vendor(enabled=False))
    monkeypatch.setattr(waterfall, "ProspeoClient", lambda: prospeo)
    monkeypatch.setattr(waterfall, "FullEnrichClient", lambda: fe)
    monkeypatch.setattr(waterfall, "VeriphoneClient", lambda *a, **k: _vendor(enabled=False))
    monkeypatch.setattr(waterfall, "BlitzClient", lambda: _vendor(enabled=False))
    monkeypatch.setattr(waterfall, "PatternGuessClient", lambda: _vendor(enabled=False))
    monkeypatch.setattr(waterfall, "SerperClient", lambda: _vendor(enabled=False))


def test_bulk_never_calls_fullenrich(monkeypatch) -> None:
    fe = _vendor(email=EmailHit(email="jane@roofco.com", source_tier="fullenrich"))
    fe.find_email_bulk.return_value = [EmailHit(email="jane@roofco.com", source_tier="fullenrich")]
    _patch_hosts(monkeypatch, fe, _vendor(email=None))
    out = waterfall.enrich_waterfall(
        [{"domain": "roofco.com", "first_name": "Jane", "last_name": "Smith"}],
        client_tag="peterson",
        need="email",
        max_tier="fullenrich",
        skip_tiers="fullenrich",
        write_supabase=False,
        parallel=False,
    )
    fe.find_email.assert_not_called()
    fe.find_email_bulk.assert_not_called()
    fe.find_mobile.assert_not_called()
    assert out["max_tier"] == "prospeo"
    assert "fullenrich is reply-handler only; capped at prospeo" in out["warnings"]
    assert out["vendors_enabled"]["fullenrich"] is False


def test_bulk_estimate_shows_fullenrich_not_allowed(monkeypatch) -> None:
    _patch_hosts(monkeypatch, _vendor(), _vendor())
    out = waterfall.enrich_waterfall(
        [{"company_name": "Roof Co", "first_name": "Jane", "last_name": "Smith"}],
        client_tag="peterson",
        need="email",
        max_tier="fullenrich",
        estimate_only=True,
        write_supabase=False,
    )
    assert out["estimate"]["fullenrich"]["allowed"] is False
    assert out["estimate"]["fullenrich"]["usd_est"] == 0.0
    assert "fullenrich" not in out["tiers_by_mode"]["name_company"]


def test_enrich_person_still_calls_fullenrich(monkeypatch) -> None:
    fe = _vendor(email=EmailHit(email="jane@roofco.com", source_tier="fullenrich"))
    _patch_hosts(monkeypatch, fe, _vendor(email=None))
    hit = waterfall.enrich_one_person(
        client_tag="replyhandler",
        first_name="Jane",
        last_name="Smith",
        domain="roofco.com",
        need="email",
        write_supabase=False,
    )
    fe.find_email.assert_called_once()
    assert hit["email_tier"] == "fullenrich"


def test_estimate_counts_blitz_records_for_it_rows(monkeypatch) -> None:
    _patch_hosts(monkeypatch, _vendor(), _vendor())
    out = waterfall.enrich_waterfall(
        [
            {"domain": "a.com", "first_name": "J", "last_name": "S", "title": "CIO", "linkedin_url": "https://www.linkedin.com/in/js"},
            {"domain": "b.com", "first_name": "K", "last_name": "T", "title": "Owner", "linkedin_url": "https://www.linkedin.com/in/kt"},
        ],
        client_tag="peterson",
        need="email",
        estimate_only=True,
        write_supabase=False,
    )
    assert out["estimate"]["blitz"]["rows"] == 1
    assert out["estimate"]["blitz"]["records_est"] == 1.0
    assert out["estimate"]["blitz"]["usd_est"] == 0.0
    assert out["tiers_by_mode"]["domain"][0] == "blitz"
