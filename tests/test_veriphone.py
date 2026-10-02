"""Veriphone mobile check on need=phone only."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from email_waterfall import waterfall
from email_waterfall.need import NeedViolation, using_need
from email_waterfall.vendors.base import EmailHit, PersonHit, PhoneHit
from email_waterfall.vendors.veriphone import VeriphoneClient
from tests.test_waterfall import _patch_clients, _patch_writes, _vendor


class _Resp:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _veriphone(*, mobile: bool = True, e164: str = "+12015550100"):
    m = MagicMock()
    m.enabled = True
    m.calls = 0
    m.hits = 0
    m.errors = 0
    m.check_mobile.return_value = (
        PhoneHit(phone=e164, source_tier="veriphone", raw={"phone_type": "mobile"})
        if mobile
        else None
    )
    return m


def test_verify_mobile_uses_e164(monkeypatch) -> None:
    client = VeriphoneClient(api_key="test-key")

    def fake_get(tier, url, **kwargs):
        assert tier == "veriphone"
        assert "/v2/verify" in url
        assert "default_country=US" in url
        assert kwargs["headers"]["Authorization"] == "Bearer test-key"
        return _Resp(
            200,
            {
                "status": "success",
                "phone_valid": True,
                "phone_type": "mobile",
                "e164": "+14152007986",
            },
        )

    monkeypatch.setattr(
        "email_waterfall.vendors.veriphone.http_client.get", fake_get
    )
    with using_need("phone"):
        hit = client.check_mobile("415-200-7986")
    assert hit is not None
    assert hit.phone == "+14152007986"
    assert client.hits == 1
    assert client.calls == 1


def test_verify_fixed_line_is_not_mobile(monkeypatch) -> None:
    client = VeriphoneClient(api_key="test-key")

    def fake_get(tier, url, **kwargs):
        return _Resp(
            200,
            {
                "status": "success",
                "phone_valid": True,
                "phone_type": "fixed_line",
                "e164": "+12025550100",
            },
        )

    monkeypatch.setattr(
        "email_waterfall.vendors.veriphone.http_client.get", fake_get
    )
    with using_need("phone"):
        assert client.check_mobile("202-555-0100") is None
    assert client.hits == 0


def test_verify_voip_is_not_mobile(monkeypatch) -> None:
    client = VeriphoneClient(api_key="test-key")

    def fake_get(tier, url, **kwargs):
        return _Resp(
            200,
            {
                "status": "success",
                "phone_valid": True,
                "phone_type": "voip",
                "e164": "+12025550111",
            },
        )

    monkeypatch.setattr(
        "email_waterfall.vendors.veriphone.http_client.get", fake_get
    )
    with using_need("phone"):
        assert client.check_mobile("202-555-0111") is None


def test_verify_raises_when_need_is_email() -> None:
    client = VeriphoneClient(api_key="test-key")
    with using_need("email"):
        with pytest.raises(NeedViolation, match="veriphone"):
            client.verify("201-555-0100")


def test_need_phone_keeps_veriphone_mobile(monkeypatch) -> None:
    sink: dict = {}
    ark = _vendor(enabled=True)
    ark.find_mobile.return_value = PhoneHit(
        phone="+19725550111", source_tier="aiark"
    )
    vp = _veriphone(mobile=True, e164="+19725550111")
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=ark,
        lm=_vendor(enabled=False),
        fe=_vendor(enabled=False),
        veriphone=vp,
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
        need="phone",
        write_supabase=True,
    )
    assert out["phones_found"] == 1
    assert out["phones_rejected_not_mobile"] == 0
    assert out["vendors_enabled"]["veriphone"] is True
    assert sink["contacts"][0]["cellphone"] == "+19725550111"
    vp.check_mobile.assert_called()
    ark.find_mobile.assert_called()


def test_need_phone_drops_landline_and_tries_next_vendor(monkeypatch) -> None:
    sink: dict = {}
    ark = _vendor(enabled=True)
    ark.find_mobile.return_value = PhoneHit(
        phone="+12025550100", source_tier="aiark"
    )
    lm = _vendor(enabled=True)
    lm.find_mobile.return_value = PhoneHit(
        phone="+19725550199", source_tier="leadmagic"
    )

    def check_mobile(phone, **kwargs):
        if "2025550100" in "".join(c for c in phone if c.isdigit()):
            return None
        return PhoneHit(phone="+19725550199", source_tier="veriphone")

    vp = _veriphone()
    vp.check_mobile.side_effect = check_mobile
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=ark,
        lm=lm,
        fe=_vendor(enabled=False),
        veriphone=vp,
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
        need="phone",
        write_supabase=True,
    )
    assert out["phones_found"] == 1
    assert out["phones_rejected_not_mobile"] == 1
    assert sink["contacts"][0]["cellphone"] == "+19725550199"
    ark.find_mobile.assert_called()
    lm.find_mobile.assert_called()


def test_need_phone_rejects_input_landline(monkeypatch) -> None:
    sink: dict = {}
    ark = _vendor(enabled=True)
    ark.find_mobile.return_value = None
    vp = _veriphone(mobile=False)
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=ark,
        lm=_vendor(enabled=False),
        fe=_vendor(enabled=False),
        veriphone=vp,
    )
    _patch_writes(monkeypatch, sink)

    out = waterfall.enrich_waterfall(
        [
            {
                "domain": "roofco.com",
                "first_name": "Jane",
                "last_name": "Smith",
                "cellphone": "201-555-0100",
            }
        ],
        client_tag="peterson",
        need="phone",
        write_supabase=True,
    )
    assert out["phones_found"] == 0
    assert out["phones_rejected_not_mobile"] >= 1
    contact = sink["contacts"][0]
    assert not contact.get("cellphone")
    ark.find_mobile.assert_called()


def test_need_both_skips_veriphone(monkeypatch) -> None:
    sink: dict = {}
    ark = _vendor(email=EmailHit(email="jane@roofco.com", source_tier="aiark"))
    ark.find_mobile.return_value = PhoneHit(
        phone="+12015550100", source_tier="aiark"
    )
    vp = _veriphone(mobile=False)
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=ark,
        lm=_vendor(enabled=True),
        fe=_vendor(enabled=False),
        veriphone=vp,
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
    assert sink["contacts"][0]["cellphone"] == "+12015550100"
    vp.check_mobile.assert_not_called()


def test_need_phone_unconfigured_keeps_number_and_warns(monkeypatch) -> None:
    sink: dict = {}
    ark = _vendor(enabled=True)
    ark.find_mobile.return_value = PhoneHit(
        phone="+19725550111", source_tier="aiark"
    )
    _patch_clients(
        monkeypatch,
        gl=_vendor(
            people=[
                PersonHit(
                    first_name="Jane",
                    last_name="Smith",
                    title="Owner",
                    linkedin_url="https://www.linkedin.com/in/jane-smith",
                    source_tier="getleads",
                )
            ]
        ),
        ark=ark,
        lm=_vendor(enabled=False),
        fe=_vendor(enabled=False),
        veriphone=_vendor(enabled=False),
    )
    _patch_writes(monkeypatch, sink)

    out = waterfall.enrich_waterfall(
        [{"domain": "roofco.com", "company_name": "Roof Co"}],
        client_tag="peterson",
        need="phone",
        write_supabase=True,
    )
    assert out["phones_found"] == 1
    assert "veriphone_unconfigured" in out["warnings"]
    assert sink["contacts"][0]["cellphone"] == "+19725550111"
