"""FullEnrich last-tier email and cellphone."""

from __future__ import annotations

from email_waterfall.need import NeedViolation, using_need
from email_waterfall.vendors.fullenrich import (
    FullEnrichClient,
    _phone_from_contact,
    _phone_value,
    _work_email_from_contact,
)


class _Resp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


def _finished_contact(contact: dict) -> dict:
    return {
        "status": "FINISHED",
        "datas": [contact],
    }


def test_phone_value_skips_landline() -> None:
    assert _phone_value({"number": "+12015550100", "line_type": "MOBILE"}) == "+12015550100"
    assert _phone_value({"number": "+12015550999", "line_type": "LANDLINE"}) == ""
    assert _phone_value("+12015550111") == "+12015550111"
    assert _phone_value("123") == ""


def test_phone_from_contact_prefers_most_probable() -> None:
    contact = {
        "contact_info": {
            "most_probable_phone": {"number": "+19725550100", "line_type": "MOBILE"},
            "phones": [{"number": "+19725550999", "line_type": "LANDLINE"}],
        }
    }
    assert _phone_from_contact(contact) == "+19725550100"


def test_phone_from_contact_skips_landline_most_probable() -> None:
    contact = {
        "contact_info": {
            "most_probable_phone": {"number": "+19725550999", "line_type": "LANDLINE"},
            "phones": [{"number": "+19725550100", "line_type": "MOBILE"}],
        }
    }
    assert _phone_from_contact(contact) == "+19725550100"


def test_work_email_from_most_probable() -> None:
    contact = {
        "contact_info": {
            "most_probable_work_email": {"email": "Jane@RoofCo.com"},
        }
    }
    assert _work_email_from_contact(contact) == "jane@roofco.com"


def test_find_email_also_returns_phone_when_need_allows(monkeypatch) -> None:
    posts: list[dict] = []
    client = FullEnrichClient(api_key="fe_test")

    def fake_post(tier, url, json=None, headers=None, timeout=60):
        posts.append(json)
        assert "contact.work_emails" in json["data"][0]["enrich_fields"]
        assert "contact.phones" in json["data"][0]["enrich_fields"]
        return _Resp(200, {"enrichment_id": "enr_1"})

    def fake_get(tier, url, headers=None, timeout=60):
        return _Resp(
            200,
            _finished_contact(
                {
                    "custom": {"idx": "0"},
                    "contact_info": {
                        "most_probable_work_email": {"email": "jane@roofco.com"},
                        "most_probable_phone": {
                            "number": "+12015550100",
                            "line_type": "MOBILE",
                        },
                    },
                }
            ),
        )

    monkeypatch.setattr("email_waterfall.http_client.post", fake_post)
    monkeypatch.setattr("email_waterfall.http_client.get", fake_get)
    monkeypatch.setattr("email_waterfall.vendors.fullenrich.time.sleep", lambda *_: None)

    hit = client.find_email("Jane", "Smith", "roofco.com", "Roof Co")
    assert hit is not None
    assert hit.email == "jane@roofco.com"
    assert hit.phone == "+12015550100"
    assert hit.source_tier == "fullenrich"
    assert posts


def test_find_email_need_email_omits_phones(monkeypatch) -> None:
    fields: list[list[str]] = []
    client = FullEnrichClient(api_key="fe_test")

    def fake_post(tier, url, json=None, headers=None, timeout=60):
        fields.append(json["data"][0]["enrich_fields"])
        return _Resp(200, {"enrichment_id": "enr_2"})

    def fake_get(tier, url, headers=None, timeout=60):
        return _Resp(
            200,
            _finished_contact(
                {
                    "custom": {"idx": "0"},
                    "contact_info": {
                        "most_probable_work_email": {"email": "jane@roofco.com"},
                    },
                }
            ),
        )

    monkeypatch.setattr("email_waterfall.http_client.post", fake_post)
    monkeypatch.setattr("email_waterfall.http_client.get", fake_get)
    monkeypatch.setattr("email_waterfall.vendors.fullenrich.time.sleep", lambda *_: None)

    with using_need("email"):
        hit = client.find_email("Jane", "Smith", "roofco.com")
    assert hit is not None
    assert hit.email == "jane@roofco.com"
    assert fields == [["contact.work_emails"]]


def test_find_mobile_contact_phones(monkeypatch) -> None:
    client = FullEnrichClient(api_key="fe_test")

    def fake_post(tier, url, json=None, headers=None, timeout=60):
        assert json["data"][0]["enrich_fields"] == ["contact.phones"]
        assert json["data"][0]["first_name"] == "Jane"
        return _Resp(200, {"enrichment_id": "enr_3"})

    def fake_get(tier, url, headers=None, timeout=60):
        return _Resp(
            200,
            _finished_contact(
                {
                    "custom": {"idx": "0"},
                    "contact_info": {
                        "phones": [{"number": "+12015550100", "line_type": "MOBILE"}],
                    },
                }
            ),
        )

    monkeypatch.setattr("email_waterfall.http_client.post", fake_post)
    monkeypatch.setattr("email_waterfall.http_client.get", fake_get)
    monkeypatch.setattr("email_waterfall.vendors.fullenrich.time.sleep", lambda *_: None)

    hit = client.find_mobile("Jane", "Smith", "roofco.com", "Roof Co")
    assert hit is not None
    assert hit.phone == "+12015550100"
    assert hit.source_tier == "fullenrich"


def test_find_mobile_need_email_raises() -> None:
    client = FullEnrichClient(api_key="fe_test")
    with using_need("email"):
        try:
            client.find_mobile("Jane", "Smith", "roofco.com")
        except NeedViolation:
            return
        raise AssertionError("expected NeedViolation")


# --- why a mobile lookup came back empty (card line in ReplyHandler) ---------


def _run_mobile(monkeypatch, *, post, get, max_wait: float = 180.0):
    client = FullEnrichClient(api_key="fe_test")
    monkeypatch.setattr("email_waterfall.http_client.post", post)
    monkeypatch.setattr("email_waterfall.http_client.get", get)
    monkeypatch.setattr("email_waterfall.vendors.fullenrich.time.sleep", lambda *_: None)
    hit = client.find_mobile("Jane", "Smith", "roofco.com", "Roof Co", max_wait=max_wait)
    return client, hit


def _ok_post(tier, url, json=None, headers=None, timeout=60):
    return _Resp(200, {"enrichment_id": "enr_x"})


def test_mobile_outcome_found(monkeypatch) -> None:
    def fake_get(tier, url, headers=None, timeout=60):
        return _Resp(200, _finished_contact({
            "custom": {"idx": "0"},
            "contact_info": {"phones": [{"number": "+12015550100", "line_type": "MOBILE"}]},
        }))

    client, hit = _run_mobile(monkeypatch, post=_ok_post, get=fake_get)
    assert hit is not None
    assert client.mobile_outcome == "found"
    assert client.credits_exhausted is False


def test_mobile_outcome_no_match_when_finished_empty(monkeypatch) -> None:
    def fake_get(tier, url, headers=None, timeout=60):
        return _Resp(200, _finished_contact({"custom": {"idx": "0"}, "contact_info": {}}))

    client, hit = _run_mobile(monkeypatch, post=_ok_post, get=fake_get)
    assert hit is None
    assert client.mobile_outcome == "no_match"
    assert client.credits_exhausted is False


def test_mobile_outcome_out_of_credits_on_post_402(monkeypatch) -> None:
    def post_402(tier, url, json=None, headers=None, timeout=60):
        return _Resp(402, {"code": "error.credits.insufficient"})

    def fake_get(tier, url, headers=None, timeout=60):
        raise AssertionError("must not poll after a 402")

    client, hit = _run_mobile(monkeypatch, post=post_402, get=fake_get)
    assert hit is None
    assert client.mobile_outcome == "out_of_credits"
    assert client.credits_exhausted is True


def test_mobile_outcome_out_of_credits_on_poll_402_returns_fast(monkeypatch) -> None:
    gets = []

    def fake_get(tier, url, headers=None, timeout=60):
        gets.append(url)
        return _Resp(402, {"status": "CREDITS_INSUFFICIENT", "data": []})

    client, hit = _run_mobile(monkeypatch, post=_ok_post, get=fake_get)
    assert hit is None
    assert len(gets) == 1  # used to spin until max_wait on any 4xx
    assert client.mobile_outcome == "out_of_credits"
    assert client.credits_exhausted is True


def test_mobile_outcome_out_of_credits_on_status_field(monkeypatch) -> None:
    def fake_get(tier, url, headers=None, timeout=60):
        return _Resp(200, {"status": "CREDITS_INSUFFICIENT", "datas": []})

    client, hit = _run_mobile(monkeypatch, post=_ok_post, get=fake_get)
    assert hit is None
    assert client.mobile_outcome == "out_of_credits"


def test_poll_402_keeps_contacts_finished_before_credits_ran_out(monkeypatch) -> None:
    def fake_get(tier, url, headers=None, timeout=60):
        return _Resp(402, {
            "status": "CREDITS_INSUFFICIENT",
            "datas": [{
                "custom": {"idx": "0"},
                "contact_info": {"phones": [{"number": "+12015550100", "line_type": "MOBILE"}]},
            }],
        })

    client, hit = _run_mobile(monkeypatch, post=_ok_post, get=fake_get)
    assert hit is not None and hit.phone == "+12015550100"
    assert client.mobile_outcome == "found"
    assert client.credits_exhausted is True


def test_mobile_outcome_timeout_when_never_finishes(monkeypatch) -> None:
    def fake_get(tier, url, headers=None, timeout=60):
        return _Resp(200, {"status": "IN_PROGRESS", "datas": []})

    client, hit = _run_mobile(monkeypatch, post=_ok_post, get=fake_get, max_wait=0.0)
    assert hit is None
    assert client.mobile_outcome == "timeout"


def test_mobile_outcome_error_on_failed_status_and_http_500(monkeypatch) -> None:
    def failed_get(tier, url, headers=None, timeout=60):
        return _Resp(200, {"status": "FAILED", "datas": []})

    client, _ = _run_mobile(monkeypatch, post=_ok_post, get=failed_get)
    assert client.mobile_outcome == "error"

    def post_500(tier, url, json=None, headers=None, timeout=60):
        return _Resp(500, {})

    client, _ = _run_mobile(monkeypatch, post=post_500, get=failed_get)
    assert client.mobile_outcome == "error"
    assert client.credits_exhausted is False
