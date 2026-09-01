"""LeadMagic mobile-finder parsing."""

from __future__ import annotations

from email_waterfall.vendors.leadmagic import LeadMagicClient


def test_find_mobile_profile_and_work_email(monkeypatch) -> None:
    client = LeadMagicClient(api_key="lm_test")
    posts: list[tuple[str, dict]] = []

    def fake_post(path, body):
        posts.append((path, body))
        return {
            "profile_url": body.get("profile_url"),
            "mobile_number": "+1-555-123-4567",
            "credits_consumed": 5,
            "message": "Mobile number found.",
        }

    monkeypatch.setattr(client, "_post", fake_post)
    hit = client.find_mobile(
        linkedin_url="https://www.linkedin.com/in/johndoe",
        work_email="john@company.com",
    )
    assert hit is not None
    assert hit.phone == "+1-555-123-4567"
    assert hit.source_tier == "leadmagic"
    assert posts[0][0] == "/v1/people/mobile-finder"
    assert posts[0][1]["profile_url"] == "https://www.linkedin.com/in/johndoe"
    assert posts[0][1]["work_email"] == "john@company.com"


def test_find_mobile_not_found(monkeypatch) -> None:
    client = LeadMagicClient(api_key="lm_test")

    def fake_post(path, body):
        return {
            "mobile_number": None,
            "credits_consumed": 0,
            "message": "mobile not found",
        }

    monkeypatch.setattr(client, "_post", fake_post)
    assert client.find_mobile(work_email="jane@roofco.com") is None


def test_find_mobile_requires_identifier() -> None:
    client = LeadMagicClient(api_key="lm_test")
    assert client.find_mobile() is None
