from types import SimpleNamespace as NS

from email_waterfall import call_ready as cr
from email_waterfall.vendors.base import PhoneHit, PersonHit


class FakeVP:
    enabled = True

    def __init__(self, table):
        self.table = table
        self.calls = 0

    def verify(self, phone):
        self.calls += 1
        valid, typ = self.table.get(cr.norm_phone(phone), (False, ""))
        return NS(phone_valid=valid, phone_type=typ, e164="+" + cr.norm_phone(phone), phone=phone)


class FakeFE:
    enabled = True

    def __init__(self, phone=None):
        self.phone = phone

    def find_mobile(self, *a, **k):
        if not self.phone:
            return None
        return PhoneHit(phone=self.phone, source_tier="fullenrich",
                        raw={"linkedin_url": "https://linkedin.com/in/jane-doe-1",
                             "full_name": "Jane Doe", "company_name": "Acme Roofing"})


class FakeAI:
    enabled = True

    def find_people(self, domain, **k):
        return [PersonHit(first_name="Jane", last_name="Smith", title="Owner",
                          linkedin_url="https://linkedin.com/in/jane-smith")]


def run(**kw):
    base = dict(client_tag="t", first_name="Jane", last_name="Doe", domain="acmeroofing.com",
                company_name="Acme Roofing", title="Owner", use_cache=False,
                verdict_fn=lambda p: {"icp_verdict": "yes", "icp_reason": "fits", "dm_verdict": "yes",
                                      "dm_reason": "owner", "cost_usd": 0.002},
                waterfall_phone_fn=lambda **k: {"phone": "", "spend": 0.0})
    base.update(kw)
    return cr.enrich_call_ready(**base)


def test_fullenrich_mobile_first_and_verified():
    vp = FakeVP({"15555550101": (True, "mobile")})
    out = run(vendors={"fullenrich": FakeFE("555-555-0101"), "veriphone": vp, "aiark": FakeAI()})
    assert out["phone_verified"] and out["phone_type"] == "mobile"
    assert out["phone_source"] == "fullenrich"
    assert out["linkedin_url"].endswith("/jane-doe-1")
    assert out["linkedin_confidence"] in {"high", "medium"}
    assert out["icp_verdict"] == "yes" and out["dm_verdict"] == "yes"
    assert 0 < out["cost_usd"] <= 5.0


def test_falls_back_to_known_phones_and_ranks_mobile():
    vp = FakeVP({"15555550102": (True, "fixed_line"), "15555550103": (True, "mobile")})
    out = run(vendors={"fullenrich": FakeFE(None), "veriphone": vp},
              known_phones=[{"phone": "5555550102", "source": "smartlead"},
                            {"phone": "5555550103", "source": "heyreach"}])
    assert out["phone"].endswith("5555550103") and out["phone_source"] == "heyreach"


def test_wrong_person_linkedin_is_review():
    out = run(vendors={"fullenrich": FakeFE(None), "veriphone": FakeVP({}), "aiark": FakeAI()})
    assert out["linkedin_confidence"] == "review"
    assert out["phone_verified"] is False


def test_sales_rep_is_never_dm():
    out = run(title="Account Executive", vendors={"fullenrich": FakeFE(None), "veriphone": FakeVP({})})
    assert out["dm_verdict"] == "no"


def test_cap_blocks_fullenrich():
    out = run(approve_cost_usd=0.1, vendors={"fullenrich": FakeFE("5555550101"), "veriphone": FakeVP({})})
    assert "fullenrich_phone" in out["skipped_for_cap"]
    assert out["cost_usd"] <= 0.1


def test_cap_never_above_five():
    assert cr.Budget(50).cap == 5.0
