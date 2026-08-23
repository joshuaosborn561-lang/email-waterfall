"""Smartlead plan email finder + credit consumption."""

from __future__ import annotations

from email_waterfall.vendors.smartlead import SmartleadClient


class _Resp:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_find_email_name_domain(monkeypatch) -> None:
    client = SmartleadClient(api_key="sl_test")
    client._credits_available = 10
    client._checked_at = 10**9

    def fake_post(tier, url, json=None, headers=None, timeout=45):
        assert tier == "smartlead"
        assert "find-emails" in url
        assert "api_key=sl_test" in url
        assert json["contacts"][0]["firstName"] == "Jane"
        assert json["contacts"][0]["companyDomain"] == "roofco.com"
        return _Resp(
            200,
            {
                "success": True,
                "data": [
                    {
                        "firstName": "Jane",
                        "lastName": "Smith",
                        "companyDomain": "roofco.com",
                        "email_id": "Jane@RoofCo.com",
                        "status": "Found",
                        "verification_status": "Valid",
                    }
                ],
            },
        )

    monkeypatch.setattr("email_waterfall.http_client.post", fake_post)
    hit = client.find_email("Jane", "Smith", "roofco.com", "Roof Co")
    assert hit is not None
    assert hit.email == "jane@roofco.com"
    assert hit.source_tier == "smartlead"
    assert client.credit_snapshot()["available"] == 9


def test_find_email_not_found_still_spends_credit(monkeypatch) -> None:
    client = SmartleadClient(api_key="sl_test")
    client._credits_available = 3
    client._checked_at = 10**9

    monkeypatch.setattr(
        "email_waterfall.http_client.post",
        lambda *a, **k: _Resp(
            200,
            {
                "success": True,
                "data": [
                    {
                        "email_id": "",
                        "status": "Not Found",
                        "verification_status": None,
                    }
                ],
            },
        ),
    )
    assert client.find_email("Jane", "Smith", "roofco.com") is None
    assert client.credit_snapshot()["available"] == 2
    assert client.credit_snapshot()["exhausted"] is False


def test_skips_when_credits_exhausted(monkeypatch) -> None:
    client = SmartleadClient(api_key="sl_test")
    called = {"n": 0}

    def fake_get(tier, url, headers=None, timeout=45):
        called["n"] += 1
        return _Resp(
            200,
            {
                "success": True,
                "data": {"availableCredits": {"available": 0, "total": 1000, "used": 1000}},
            },
        )

    def boom(*a, **k):
        raise AssertionError("find-emails must not run when allotment is spent")

    monkeypatch.setattr("email_waterfall.http_client.get", fake_get)
    monkeypatch.setattr("email_waterfall.http_client.post", boom)
    assert client.find_email("Jane", "Smith", "roofco.com") is None
    assert client.enabled is False
    assert called["n"] == 1


def test_402_marks_exhausted(monkeypatch) -> None:
    client = SmartleadClient(api_key="sl_test")
    client._credits_available = 1
    client._checked_at = 10**9

    monkeypatch.setattr(
        "email_waterfall.http_client.post",
        lambda *a, **k: _Resp(402, {"success": False, "message": "Payment Required"}),
    )
    assert client.find_email("Jane", "Smith", "roofco.com") is None
    assert client.credit_snapshot()["exhausted"] is True
    assert client.enabled is False


def test_refresh_credits_from_analytics(monkeypatch) -> None:
    client = SmartleadClient(api_key="sl_test")

    monkeypatch.setattr(
        "email_waterfall.http_client.get",
        lambda *a, **k: _Resp(
            200,
            {
                "success": True,
                "data": {"availableCredits": {"available": 42, "total": 100, "used": 58}},
            },
        ),
    )
    assert client.refresh_credits(force=True) == 42
    snap = client.credit_snapshot()
    assert snap["total"] == 100
    assert snap["used"] == 58
    assert snap["exhausted"] is False


def test_rejects_invalid_verification(monkeypatch) -> None:
    client = SmartleadClient(api_key="sl_test")
    client._credits_available = 5
    client._checked_at = 10**9
    monkeypatch.setattr(
        "email_waterfall.http_client.post",
        lambda *a, **k: _Resp(
            200,
            {
                "success": True,
                "data": [
                    {
                        "email_id": "jane@roofco.com",
                        "status": "Found",
                        "verification_status": "Invalid",
                    }
                ],
            },
        ),
    )
    assert client.find_email("Jane", "Smith", "roofco.com") is None
