"""Prospeo enrich-person email parsing."""

from __future__ import annotations

from email_waterfall.vendors.prospeo import ProspeoClient


def _patch_post(monkeypatch, fake_post):
    monkeypatch.setattr("email_waterfall.http_client.post", fake_post)


def test_find_email_name_domain(monkeypatch) -> None:
    client = ProspeoClient(api_key="pk_test")

    def fake_post(tier, url, json=None, headers=None, timeout=45):
        assert tier == "prospeo"
        assert url.endswith("/enrich-person")
        assert headers["X-KEY"] == "pk_test"
        assert json["only_verified_email"] is True
        assert json["data"]["first_name"] == "Jane"
        assert json["data"]["company_website"] == "roofco.com"

        class Resp:
            status_code = 200

            def json(self):
                return {
                    "error": False,
                    "person": {
                        "email": {
                            "status": "VERIFIED",
                            "revealed": True,
                            "email": "Jane@RoofCo.com",
                        }
                    },
                }

        return Resp()

    _patch_post(monkeypatch, fake_post)
    hit = client.find_email("Jane", "Smith", "roofco.com", "Roof Co")
    assert hit is not None
    assert hit.email == "jane@roofco.com"
    assert hit.source_tier == "prospeo"


def test_find_email_rejects_masked(monkeypatch) -> None:
    client = ProspeoClient(api_key="pk_test")

    def fake_post(tier, url, json=None, headers=None, timeout=45):
        class Resp:
            status_code = 200

            def json(self):
                return {
                    "error": False,
                    "person": {
                        "email": {"status": "VERIFIED", "email": "jane.*****@roofco.com"}
                    },
                }

        return Resp()

    _patch_post(monkeypatch, fake_post)
    assert client.find_email("Jane", "Smith", "roofco.com") is None


def test_find_email_no_match(monkeypatch) -> None:
    client = ProspeoClient(api_key="pk_test")

    def fake_post(tier, url, json=None, headers=None, timeout=45):
        class Resp:
            status_code = 400

            def json(self):
                return {"error": True, "error_code": "NO_MATCH"}

        return Resp()

    _patch_post(monkeypatch, fake_post)
    assert client.find_email("Jane", "Smith", "roofco.com") is None


def test_find_email_linkedin_only(monkeypatch) -> None:
    client = ProspeoClient(api_key="pk_test")

    def fake_post(tier, url, json=None, headers=None, timeout=45):
        assert json["data"]["linkedin_url"].startswith("https://www.linkedin.com")

        class Resp:
            status_code = 200

            def json(self):
                return {
                    "error": False,
                    "person": {"email": {"status": "VERIFIED", "email": "pat@x.com"}},
                }

        return Resp()

    _patch_post(monkeypatch, fake_post)
    hit = client.find_email(linkedin_url="https://www.linkedin.com/in/pat-lee")
    assert hit is not None
    assert hit.email == "pat@x.com"


def test_find_mobile_enrich_mobile(monkeypatch) -> None:
    client = ProspeoClient(api_key="pk_test")

    def fake_post(tier, url, json=None, headers=None, timeout=45):
        assert json["enrich_mobile"] is True
        assert json["only_verified_email"] is False

        class Resp:
            status_code = 200

            def json(self):
                return {
                    "error": False,
                    "person": {"mobile": {"mobile": "+12015550100"}},
                }

        return Resp()

    _patch_post(monkeypatch, fake_post)
    hit = client.find_mobile("Jane", "Smith", "roofco.com")
    assert hit is not None
    assert hit.phone == "+12015550100"
    assert hit.source_tier == "prospeo"
