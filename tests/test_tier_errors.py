"""One row through each tier must not 100% error — catches getleads -32602."""

from __future__ import annotations

from email_waterfall import waterfall
from tests.test_getleads import LINKEDIN_BATCH_TOOL, ANON_EMAIL, FakeMcp, Tok
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
    lm = CountingVendor()
    sl = CountingVendor()
    prospeo = CountingVendor()
    fe = CountingVendor()
    _patch_clients(
        monkeypatch, gl=gl, ark=ark, lm=lm, fe=fe, prospeo=prospeo, smartlead=sl
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
    for vendor in (ark, lm, sl, prospeo, fe):
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
        max_tier="leadmagic",
        write_supabase=True,
    )
    assert mcp.calls == []
    assert gl.calls == 0
    assert gl.errors == 0
    _assert_no_all_error_tiers(out["tier_stats"])


def test_all_error_tier_is_detected() -> None:
    assert _all_error_tiers(
        {
            "getleads": {"vendor_calls": 3, "errors": 3},
            "aiark": {"vendor_calls": 1, "errors": 0},
        }
    ) == ["getleads (3/3)"]
