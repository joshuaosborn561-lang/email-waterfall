"""AI Ark email export/single parsing and identifier routing."""

from __future__ import annotations

from email_waterfall.vendors.ai_ark import AiArkClient, _pick_email


def test_pick_email_v2_envelope() -> None:
    email, status = _pick_email(
        {
            "status": 200,
            "error": None,
            "data": {
                "id": "abc",
                "email": {"value": "Pat@ParagonHonda.com", "state": "DONE"},
            },
        }
    )
    assert email == "pat@paragonhonda.com"
    assert status == "DONE"


def test_pick_email_v2_miss_is_empty() -> None:
    email, status = _pick_email(
        {
            "status": 404,
            "error": {"error": "no email found"},
            "data": None,
        }
    )
    assert email == ""
    assert status == ""


def test_pick_email_v1_output_list() -> None:
    email, _status = _pick_email(
        {
            "email": {
                "output": [
                    {"found": False, "address": "skip@x.com"},
                    {"found": True, "address": "ok@x.com", "status": "VALID"},
                ]
            }
        }
    )
    assert email == "ok@x.com"


def test_find_email_uses_linkedin_export_single(monkeypatch) -> None:
    client = AiArkClient(api_key="tok")
    posts: list[tuple[str, dict]] = []

    def fake_post(path, body):
        posts.append((path, body))
        if path.endswith("/v2/people/export/single"):
            return 200, {
                "status": 200,
                "data": {"email": {"value": "pat@paragonhonda.com", "state": "DONE"}},
            }
        return 200, {"content": []}

    monkeypatch.setattr(client, "_post", fake_post)
    hit = client.find_email(
        linkedin_url="https://www.linkedin.com/in/pat-lee",
        domain="paragonhonda.com",
    )
    assert hit is not None
    assert hit.email == "pat@paragonhonda.com"
    assert hit.source_tier == "aiark"
    assert posts[0][0] == "/v2/people/export/single"
    assert posts[0][1]["url"] == "https://www.linkedin.com/in/pat-lee"


def test_find_email_name_domain_searches_then_exports(monkeypatch) -> None:
    client = AiArkClient(api_key="tok")

    def fake_post(path, body):
        if path.endswith("/v1/people"):
            assert body["account"]["domain"]["any"]["include"] == ["paragonhonda.com"]
            assert "Pat Lee" in body["contact"]["fullName"]["any"]["include"]["content"]
            return 200, {
                "content": [
                    {
                        "id": "person-9",
                        "profile": {
                            "first_name": "Pat",
                            "last_name": "Lee",
                            "title": "Service Director",
                        },
                    }
                ]
            }
        if path.endswith("/v2/people/export/single"):
            assert body["id"] == "person-9"
            return 200, {
                "data": {"email": {"value": "pat@paragonhonda.com", "state": "DONE"}}
            }
        raise AssertionError(path)

    monkeypatch.setattr(client, "_post", fake_post)
    hit = client.find_email("Pat", "Lee", "paragonhonda.com")
    assert hit is not None
    assert hit.email == "pat@paragonhonda.com"


def test_find_email_phone_can_search(monkeypatch) -> None:
    client = AiArkClient(api_key="tok")

    def fake_post(path, body):
        if path.endswith("/v1/people"):
            assert body["contact"]["keyword"]["any"]["include"]["content"] == ["2015550100"]
            return 200, {
                "content": [
                    {
                        "id": "person-phone",
                        "profile": {"first_name": "Pat", "last_name": "Lee"},
                    }
                ]
            }
        return 200, {"data": {"email": {"value": "pat@x.com", "state": "DONE"}}}

    monkeypatch.setattr(client, "_post", fake_post)
    hit = client.find_email(phone="2015550100", domain="paragonhonda.com")
    assert hit is not None
    assert hit.email == "pat@x.com"
