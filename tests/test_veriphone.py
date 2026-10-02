"""Veriphone mobile check on need=phone only."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from email_waterfall import waterfall
from email_waterfall.need import NeedViolation, using_need
from email_waterfall.vendors.base import EmailHit, PersonHit, PhoneHit
from email_waterfall.vendors.veriphone import VeriphoneClient, VeriphoneResult
from tests.test_waterfall import _patch_clients, _patch_writes, _vendor


class _Resp:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _veriphone(
    *,
    mobile: bool = True,
    e164: str = "+12015550100",
    phone_type: str | None = None,
):
    ptype = phone_type or ("mobile" if mobile else "fixed_line")
    result = VeriphoneResult(
        phone=e164,
        phone_valid=True,
        phone_type=ptype,
        e164=e164,
        raw={"phone_type": ptype, "phone_valid": True, "e164": e164},
    )
    m = MagicMock()
    m.enabled = True
    m.calls = 0
    m.hits = 0
    m.errors = 0
    m.verify.return_value = result
    m.check_mobile.return_value = (
        PhoneHit(phone=e164, source_tier="veriphone", raw=result.raw)
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
    assert sink["contacts"][0]["line_type"] == "mobile"
    vp.verify.assert_called()
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

    def verify(phone, **kwargs):
        if "2025550100" in "".join(c for c in phone if c.isdigit()):
            return VeriphoneResult(
                phone=phone,
                phone_valid=True,
                phone_type="fixed_line",
                e164="+12025550100",
                raw={"phone_type": "fixed_line", "phone_valid": True},
            )
        return VeriphoneResult(
            phone=phone,
            phone_valid=True,
            phone_type="mobile",
            e164="+19725550199",
            raw={"phone_type": "mobile", "phone_valid": True, "e164": "+19725550199"},
        )

    vp = _veriphone()
    vp.verify.side_effect = verify
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
    assert sink["contacts"][0]["line_type"] == "mobile"
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
    assert contact.get("line_type") == "fixed_line"
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
    vp.verify.assert_not_called()
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


def test_need_phone_writes_source_phone_and_line_type(monkeypatch) -> None:
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
    writebacks: list[dict] = []
    monkeypatch.setattr(
        waterfall.table_source,
        "fetch_source_rows",
        lambda src: [
            {
                "_source_key": 4,
                "first_name": "Jane",
                "last_name": "Smith",
                "company_name": "Roof Co",
                "domain": "roofco.com",
            }
        ],
    )
    monkeypatch.setattr(waterfall.table_source, "ensure_writeback_columns", lambda src: [])
    monkeypatch.setattr(
        waterfall.table_source,
        "writeback_result",
        lambda src, **kwargs: writebacks.append(kwargs),
    )

    out = waterfall.enrich_waterfall(
        client_tag="peterson",
        need="phone",
        write_supabase=True,
        source={"table": "ew_names_ready"},
    )
    assert out["phones_found"] == 1
    assert writebacks[0]["phone"] == "+19725550111"
    assert writebacks[0]["phone_type"] == "mobile"
    assert sink["contacts"][0]["line_type"] == "mobile"


def test_verify_only_skips_finders_and_writes_verdict(monkeypatch) -> None:
    sink: dict = {}
    ark = _vendor(enabled=True)
    ark.find_mobile.return_value = PhoneHit(
        phone="+19725550999", source_tier="aiark"
    )
    vp = _veriphone(mobile=True, e164="+14155552671")
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=ark,
        lm=_vendor(enabled=True),
        fe=_vendor(enabled=False),
        veriphone=vp,
    )
    _patch_writes(monkeypatch, sink)
    writebacks: list[dict] = []
    monkeypatch.setattr(
        waterfall.table_source,
        "fetch_source_rows",
        lambda src: [
            {
                "_source_key": 8,
                "first_name": "Jane",
                "last_name": "Smith",
                "company_name": "Roof Co",
                "domain": "roofco.com",
                "phone": "415-555-2671",
            }
        ],
    )
    monkeypatch.setattr(waterfall.table_source, "ensure_writeback_columns", lambda src: [])
    monkeypatch.setattr(
        waterfall.table_source,
        "writeback_result",
        lambda src, **kwargs: writebacks.append(kwargs),
    )

    out = waterfall.enrich_waterfall(
        client_tag="peterson",
        need="phone",
        verify_only=True,
        write_supabase=True,
        source={"table": "ew_names_ready"},
    )
    assert out["verify_only"] is True
    assert out["phones_found"] == 1
    ark.find_mobile.assert_not_called()
    vp.verify.assert_called()
    assert sink["contacts"][0]["cellphone"] == "+14155552671"
    assert sink["contacts"][0]["line_type"] == "mobile"
    assert writebacks[0]["phone"] == "+14155552671"
    assert writebacks[0]["phone_type"] == "mobile"


def test_verify_only_records_landline_without_finder(monkeypatch) -> None:
    sink: dict = {}
    ark = _vendor(enabled=True)
    ark.find_mobile.return_value = PhoneHit(
        phone="+19725550999", source_tier="aiark"
    )
    vp = _veriphone(mobile=False, e164="+12025550100", phone_type="fixed_line")
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
                "phone": "202-555-0100",
            }
        ],
        client_tag="peterson",
        need="phone",
        verify_only=True,
        write_supabase=True,
    )
    ark.find_mobile.assert_not_called()
    assert out["verify_only"] is True
    assert out["phones_rejected_not_mobile"] >= 1
    contact = sink["contacts"][0]
    assert contact["line_type"] == "fixed_line"
    assert contact.get("cellphone") == "202-555-0100"


def test_verify_only_estimate_has_no_finder_credits(monkeypatch) -> None:
    from tests.test_estimate import _mute_smartlead

    _mute_smartlead(monkeypatch)
    out = waterfall.enrich_waterfall(
        [
            {
                "domain": "roofco.com",
                "first_name": "Jane",
                "last_name": "Smith",
                "phone": "415-555-2671",
            }
        ],
        client_tag="peterson",
        need="phone",
        verify_only=True,
        estimate_only=True,
        write_supabase=False,
    )
    assert out["estimate_only"] is True
    assert out["verify_only"] is True
    assert out["estimate"] == {}
    assert out["spend"] == 0
    assert out["rows_with_phone"] == 1
