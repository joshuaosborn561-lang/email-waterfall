"""One row through each tier must not 100% error — catches getleads -32602."""

from __future__ import annotations

from email_waterfall import waterfall
from tests.test_getleads import (
    ANON_EMAIL,
    FakeMcp,
    LINKEDIN_BATCH_TOOL,
    PERSON_BATCH_TOOL,
    Tok,
)
from tests.test_waterfall import _patch_clients, _patch_writes
from email_waterfall.vendors.getleads import GetLeadsClient


class CountingVendor:
    """Miss without error, or error on every call."""

    def __init__(self, *, fail: bool = False):
        self.enabled = True
        self.calls = 0
        self.hits = 0
        self.errors = 0
        self.fail = fail

    def find_email(self, *args, **kwargs):
        self.calls += 1
        if self.fail:
            self.errors += 1
        return None

    def find_people(self, *args, **kwargs):
        return []

    def find_email_bulk(self, *args, **kwargs):
        self.calls += 1
        if self.fail:
            self.errors += 1
        return []

    def find_mobile(self, *args, **kwargs):
        return None


def _all_error_tiers(tier_stats: dict) -> list[str]:
    broken: list[str] = []
    for name, stats in tier_stats.items():
        calls = int(stats.get("vendor_calls") or stats.get("calls") or 0)
        errors = int(stats.get("errors") or 0)
        if calls > 0 and errors >= calls:
            broken.append(f"{name} ({errors}/{calls})")
    return broken


def _assert_no_all_error_tiers(tier_stats: dict) -> None:
    broken = _all_error_tiers(tier_stats)
    assert not broken, f"every call errored: {', '.join(broken)}"


def test_one_row_through_each_tier_fails_if_every_call_errors(monkeypatch) -> None:
    mcp = FakeMcp(result={"status": "not_found"}, tools=[LINKEDIN_BATCH_TOOL])
    gl = GetLeadsClient(token_manager=Tok(), mcp=mcp, tools=[LINKEDIN_BATCH_TOOL])
    ark = CountingVendor()
    sl = CountingVendor()
    prospeo = CountingVendor()
    fe = CountingVendor()
    _patch_clients(
        monkeypatch, gl=gl, ark=ark, fe=fe, prospeo=prospeo, smartlead=sl
    )
    _patch_writes(monkeypatch, {})

    out = waterfall.enrich_waterfall(
        [
            {
                "domain": "acme.test",
                "first_name": "Jane",
                "last_name": "Doe",
                "company_name": "Acme",
                "title": "Owner",
                "linkedin_url": "https://www.linkedin.com/in/jane-doe",
            }
        ],
        client_tag="peterson",
        need="email",
        max_tier="fullenrich",
        write_supabase=True,
    )
    _assert_no_all_error_tiers(out["tier_stats"])
    assert mcp.calls, "getleads should have been called with a LinkedIn URL"
    assert isinstance(mcp.calls[0][1].get("items"), list)
    assert gl.errors == 0
    assert gl.calls == 1
    for vendor in (ark, sl, prospeo, fe):
        assert vendor.calls >= 1
        assert vendor.errors == 0


def test_one_row_without_linkedin_skips_linkedin_batch(monkeypatch) -> None:
    mcp = FakeMcp(result=ANON_EMAIL, tools=[LINKEDIN_BATCH_TOOL])
    gl = GetLeadsClient(token_manager=Tok(), mcp=mcp, tools=[LINKEDIN_BATCH_TOOL])
    others = CountingVendor()
    _patch_clients(
        monkeypatch,
        gl=gl,
        ark=others,
        lm=CountingVendor(),
        fe=CountingVendor(),
        smartlead=CountingVendor(),
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
        max_tier="aiark",
        write_supabase=True,
    )
    assert mcp.calls
    assert mcp.calls[0][0] == "getleads_enrich_person_batch"
    assert isinstance(mcp.calls[0][1].get("items"), list)
    assert gl.calls == 1
    assert gl.errors == 0
    _assert_no_all_error_tiers(out["tier_stats"])


def test_all_error_tier_is_detected() -> None:
    assert _all_error_tiers(
        {
            "getleads": {"vendor_calls": 3, "errors": 3},
            "aiark": {"vendor_calls": 1, "errors": 0},
        }
    ) == ["getleads (3/3)"]


class CircuitVendor:
    enabled = True

    def __init__(self) -> None:
        self.calls = 0
        self.hits = 0
        self.errors = 0
        self.first_error = None

    def find_email(self, *args, **kwargs):
        self.calls += 1
        self.errors += 1
        if self.first_error is None:
            self.first_error = (
                'items expected array, received undefined'
            )
        return None

    def find_people(self, *args, **kwargs):
        return []

    def find_email_bulk(self, *args, **kwargs):
        return []

    def find_mobile(self, *args, **kwargs):
        return None


def test_circuit_disables_tier_after_21_of_first_25(monkeypatch) -> None:
    gl = CircuitVendor()
    others = CountingVendor()
    _patch_clients(
        monkeypatch,
        gl=gl,
        ark=others,
        lm=CountingVendor(),
        fe=CountingVendor(),
        smartlead=CountingVendor(),
    )
    _patch_writes(monkeypatch, {})
    rows = [
        {
            "domain": f"acme{i}.test",
            "first_name": "Jane",
            "last_name": "Doe",
            "company_name": "Acme",
            "title": "Owner",
        }
        for i in range(30)
    ]
    out = waterfall.enrich_waterfall(
        rows,
        client_tag="peterson",
        need="email",
        max_tier="getleads",
        write_supabase=True,
        parallel=False,
    )
    assert gl.calls == 21
    assert gl.errors == 21
    stats = out["tier_stats"]["getleads"]
    assert stats["disabled"] is True
    assert "21 errors" in stats["disabled_reason"]
    assert stats["first_error"] == "items expected array, received undefined"
    assert out["tier_breakdown"]["getleads"]["first_error"]
    assert any("getleads" in w for w in out["warnings"])


def test_getleads_credits_charged_on_hit(monkeypatch) -> None:
    mcp = FakeMcp(result=ANON_EMAIL, tools=[PERSON_BATCH_TOOL])
    gl = GetLeadsClient(token_manager=Tok(), mcp=mcp, tools=[PERSON_BATCH_TOOL])
    _patch_clients(
        monkeypatch,
        gl=gl,
        ark=CountingVendor(),
        lm=CountingVendor(),
        fe=CountingVendor(),
        smartlead=CountingVendor(),
    )
    _patch_writes(monkeypatch, {})
    out = waterfall.enrich_waterfall(
        [
            {
                "domain": "acme.test",
                "first_name": "Jane",
                "last_name": "Doe",
                "company_name": "Acme",
                "title": "Owner",
            }
        ],
        client_tag="peterson",
        need="email",
        max_tier="getleads",
        write_supabase=True,
    )
    assert gl.errors == 0
    assert gl.credits_charged == 1
    assert out["tier_stats"]["getleads"]["credits_charged"] == 1
    assert out["emails_found"] == 1
