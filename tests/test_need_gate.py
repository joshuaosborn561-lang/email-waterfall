"""need gates vendor capabilities before any phone/email HTTP call."""

from __future__ import annotations

import pytest

from email_waterfall import waterfall
from email_waterfall.need import NeedViolation, using_need
from email_waterfall.vendors.ai_ark import AiArkClient
from email_waterfall.vendors.base import EmailHit, PersonHit, PhoneHit
from email_waterfall.vendors.leadmagic import LeadMagicClient
from tests.test_waterfall import _patch_clients, _patch_writes, _vendor


def _counting_post(client: AiArkClient, impl):
    def fake_post(path, body):
        client.calls += 1
        return impl(path, body)

    return fake_post


def test_need_email_does_not_call_phone_finders(monkeypatch) -> None:
    sink: dict = {}
    ark = _vendor(email=EmailHit(email="jane@roofco.com", source_tier="aiark"))
    ark.find_mobile.return_value = PhoneHit(
        phone="+12015550100", source_tier="aiark"
    )
    lm = _vendor(enabled=True)
    lm.find_mobile.return_value = PhoneHit(
        phone="+19725550199", source_tier="leadmagic"
    )
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=ark,
        lm=lm,
        fe=_vendor(enabled=False),
    )
    _patch_writes(monkeypatch, sink)

    out = waterfall.enrich_waterfall(
        [
            {
                "domain": "roofco.com",
                "first_name": "Jane",
                "last_name": "Smith",
                "linkedin_url": "https://www.linkedin.com/in/jane-smith",
            }
        ],
        client_tag="peterson",
        need="email",
        max_tier="leadmagic",
        write_supabase=True,
    )
    assert out["need"] == "email"
    assert out["emails_found"] == 1
    assert out["phones_found"] == 0
    assert out["tier_breakdown"]["aiark"]["phone_hits"] == 0
    assert out["tier_breakdown"]["leadmagic"]["phone_hits"] == 0
    assert out["suppressed_by_need"]["aiark_phone"] == 1
    assert out["suppressed_by_need"]["leadmagic_phone"] == 1
    ark.find_email.assert_called()
    ark.find_mobile.assert_not_called()
    lm.find_mobile.assert_not_called()
    assert not sink["contacts"][0].get("cellphone")


def test_need_both_still_returns_phones(monkeypatch) -> None:
    sink: dict = {}
    ark = _vendor(email=EmailHit(email="jane@roofco.com", source_tier="aiark"))
    ark.find_mobile.return_value = PhoneHit(
        phone="+12015550100", source_tier="aiark"
    )
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=ark,
        lm=_vendor(enabled=True),
        fe=_vendor(enabled=False),
    )
    _patch_writes(monkeypatch, sink)

    out = waterfall.enrich_waterfall(
        [
            {
                "domain": "roofco.com",
                "first_name": "Jane",
                "last_name": "Smith",
            }
        ],
        client_tag="peterson",
        need="both",
        write_supabase=True,
    )
    assert out["phones_found"] == 1
    assert out["emails_found"] == 1
    assert sink["contacts"][0]["cellphone"] == "+12015550100"
    ark.find_mobile.assert_called()
    assert out["suppressed_by_need"] == {}


def test_need_dm_does_not_call_phone_or_email_finders(monkeypatch) -> None:
    sink: dict = {}
    ark = _vendor(enabled=True)
    lm = _vendor(enabled=True)
    gl = _vendor(
        people=[
            PersonHit(
                first_name="Jane",
                last_name="Smith",
                title="Owner",
                source_tier="getleads",
            )
        ]
    )
    _patch_clients(
        monkeypatch,
        gl=gl,
        ark=ark,
        lm=lm,
        fe=_vendor(enabled=False),
    )
    _patch_writes(monkeypatch, sink)

    out = waterfall.enrich_waterfall(
        [{"domain": "roofco.com", "company_name": "Roof Co"}],
        client_tag="peterson",
        need="dm",
        write_supabase=True,
    )
    assert out["dms_found"] == 1
    assert out["emails_found"] == 0
    assert out["phones_found"] == 0
    gl.find_email.assert_not_called()
    ark.find_email.assert_not_called()
    ark.find_mobile.assert_not_called()
    lm.find_mobile.assert_not_called()


def test_aiark_find_mobile_raises_when_need_is_email() -> None:
    client = AiArkClient(api_key="tok")
    with using_need("email"):
        with pytest.raises(NeedViolation, match="mobile-phone-finder"):
            client.find_mobile("Jane", "Smith", "roofco.com")


def test_leadmagic_find_mobile_raises_when_need_is_email() -> None:
    client = LeadMagicClient(api_key="lm")
    with using_need("email"):
        with pytest.raises(NeedViolation, match="mobile-finder"):
            client.find_mobile(work_email="jane@roofco.com")


def test_aiark_search_then_export_is_one_attempt(monkeypatch) -> None:
    """People Search + export/single + mobile = 1 attempt, 3 vendor_calls."""
    client = AiArkClient(api_key="tok")
    posts: list[str] = []

    def impl(path, body):
        posts.append(path)
        if path.endswith("/v1/people"):
            return 200, {
                "content": [
                    {
                        "id": "p1",
                        "profile": {"first_name": "Jane", "last_name": "Smith"},
                    }
                ]
            }
        if path.endswith("/v2/people/export/single"):
            return 200, {
                "status": 200,
                "data": {"email": {"value": "jane@roofco.com", "state": "DONE"}},
            }
        if path.endswith("/v2/people/mobile-phone-finder"):
            return 200, {"data": {"mobile": "+12015550100"}}
        return 200, {}

    monkeypatch.setattr(client, "_post", _counting_post(client, impl))
    disabled = _vendor(enabled=False)
    wf = waterfall.Waterfall(
        ai_ark=client,
        getleads=disabled,
        smartlead=disabled,
        leadmagic=disabled,
        prospeo=disabled,
        fullenrich=disabled,
        need="both",
    )
    row = {
        "first_name": "Jane",
        "last_name": "Smith",
        "domain": "roofco.com",
        "company_name": "Roof Co",
        "linkedin_url": "",
        "phone": "",
        "ai_ark_id": "",
        "full_name": "Jane Smith",
        "email": "",
        "title": "",
        "mode": "domain",
    }
    with using_need("both"):
        email_hit = wf.resolve_email(row, include_fullenrich=False)
        phone_hit = wf.resolve_phone(row, email=email_hit.email if email_hit else "")
    assert email_hit is not None
    assert email_hit.email == "jane@roofco.com"
    assert phone_hit is not None
    assert wf.tier_stats["aiark"]["calls"] == 1
    assert client.calls == 3
    assert posts.count("/v1/people") == 1
    assert posts.count("/v2/people/export/single") == 1
    assert posts.count("/v2/people/mobile-phone-finder") == 1


def test_need_email_attempts_not_doubled_by_skipped_phone(monkeypatch) -> None:
    ark = AiArkClient(api_key="tok")
    posts: list[str] = []

    def impl(path, body):
        posts.append(path)
        if path.endswith("/v2/people/export/single"):
            return 200, {
                "status": 200,
                "data": {"email": {"value": "jane@roofco.com", "state": "DONE"}},
            }
        return 200, {"content": []}

    monkeypatch.setattr(ark, "_post", _counting_post(ark, impl))
    sink: dict = {}
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=ark,
        lm=_vendor(enabled=False),
        fe=_vendor(enabled=False),
    )
    _patch_writes(monkeypatch, sink)

    out = waterfall.enrich_waterfall(
        [
            {
                "domain": "roofco.com",
                "first_name": "Jane",
                "last_name": "Smith",
                "linkedin_url": "https://www.linkedin.com/in/jane-smith",
            }
        ],
        client_tag="peterson",
        need="email",
        write_supabase=True,
        parallel=False,
    )
    assert out["emails_found"] == 1
    assert out["phones_found"] == 0
    assert out["tier_breakdown"]["aiark"]["attempts"] == 1
    assert ark.calls == 1
    assert out["tier_breakdown"]["aiark"]["vendor_calls"] == 1
    assert "/v2/people/mobile-phone-finder" not in posts
