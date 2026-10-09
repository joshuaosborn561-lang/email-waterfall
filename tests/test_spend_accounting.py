"""Recorded spend is actual billed credits: hit rates, $0 on miss."""

from __future__ import annotations

from email_waterfall import waterfall
from email_waterfall.need import (
    AIARK_EMAIL_CREDITS,
    PROSPEO_EMAIL_CREDITS,
    PROSPEO_MOBILE_CREDITS,
)
from email_waterfall.vendors.base import EmailHit, PhoneHit
from tests.test_waterfall import _patch_clients, _patch_writes, _vendor


def _usd(tier: str, credits: float) -> float:
    return waterfall.attempt_cost_usd(tier, credits=credits)


def test_aiark_email_miss_books_zero(monkeypatch) -> None:
    sink: dict = {}
    ark = _vendor(email=None)
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=ark,
        fe=_vendor(enabled=False),
        prospeo=_vendor(enabled=False),
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
        need="email",
        max_tier="aiark",
        write_supabase=True,
        parallel=False,
    )
    assert out["emails_found"] == 0
    assert out["spend"] == 0
    ark.find_email.assert_called()


def test_aiark_linkedin_email_hit_books_1_credit(monkeypatch) -> None:
    sink: dict = {}
    ark = _vendor(
        email=EmailHit(email="jane@roofco.com", source_tier="aiark")
    )
    ark.last_credits = AIARK_EMAIL_CREDITS
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=ark,
        fe=_vendor(enabled=False),
        prospeo=_vendor(enabled=False),
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
        max_tier="aiark",
        write_supabase=True,
        parallel=False,
    )
    assert out["emails_found"] == 1
    assert out["spend"] == _usd("aiark", 1.0)


def test_aiark_mobile_hit_books_5_credits(monkeypatch) -> None:
    sink: dict = {}
    ark = _vendor(email=EmailHit(email="jane@roofco.com", source_tier="aiark"))
    ark.find_mobile.return_value = PhoneHit(
        phone="+12015550100", source_tier="aiark"
    )
    # Email-by-URL 1 cr, then mobile 5 cr. last_credits is per call; the
    # mock is overwritten before each _book_vendor, so set via side_effect.
    credits = {"email": 1.0, "mobile": 5.0}

    def find_email(*_a, **_k):
        ark.last_credits = credits["email"]
        return EmailHit(email="jane@roofco.com", source_tier="aiark")

    def find_mobile(*_a, **_k):
        ark.last_credits = credits["mobile"]
        return PhoneHit(phone="+12015550100", source_tier="aiark")

    ark.find_email.side_effect = find_email
    ark.find_mobile.side_effect = find_mobile
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=ark,
        fe=_vendor(enabled=False),
        prospeo=_vendor(enabled=False),
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
        need="both",
        max_tier="aiark",
        write_supabase=True,
        parallel=False,
    )
    assert out["emails_found"] == 1
    assert out["phones_found"] == 1
    assert out["spend"] == _usd("aiark", 1.0 + 5.0)
    assert round(out["spend"], 6) == round(6.0 * 0.003667, 6)


def test_prospeo_email_miss_books_zero(monkeypatch) -> None:
    sink: dict = {}
    prospeo = _vendor(email=None)
    prospeo.last_credits = 0.0
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=_vendor(enabled=False),
        fe=_vendor(enabled=False),
        prospeo=prospeo,
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
        need="email",
        max_tier="prospeo",
        write_supabase=True,
        parallel=False,
    )
    assert out["emails_found"] == 0
    assert out["spend"] == 0
    prospeo.find_email.assert_called()


def test_prospeo_email_hit_books_1_credit(monkeypatch) -> None:
    sink: dict = {}
    prospeo = _vendor(
        email=EmailHit(email="jane@roofco.com", source_tier="prospeo")
    )
    prospeo.last_credits = PROSPEO_EMAIL_CREDITS
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=_vendor(enabled=False),
        fe=_vendor(enabled=False),
        prospeo=prospeo,
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
        need="email",
        max_tier="prospeo",
        write_supabase=True,
        parallel=False,
    )
    assert out["emails_found"] == 1
    assert out["spend"] == _usd("prospeo", 1.0)
    assert out["spend"] == round(1.0 * 0.0148, 6)


def test_prospeo_mobile_hit_books_10_credits(monkeypatch) -> None:
    sink: dict = {}
    ark = _vendor(email=EmailHit(email="jane@roofco.com", source_tier="aiark"))
    ark.find_mobile.return_value = None
    ark.last_credits = 0.0

    def find_email(*_a, **_k):
        ark.last_credits = 1.0
        return EmailHit(email="jane@roofco.com", source_tier="aiark")

    def find_mobile(*_a, **_k):
        ark.last_credits = 0.0
        return None

    ark.find_email.side_effect = find_email
    ark.find_mobile.side_effect = find_mobile
    prospeo = _vendor(enabled=True)

    def p_mobile(*_a, **_k):
        prospeo.last_credits = PROSPEO_MOBILE_CREDITS
        return PhoneHit(phone="+19725550199", source_tier="prospeo")

    prospeo.find_mobile.side_effect = p_mobile
    prospeo.last_credits = 0.0
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=ark,
        fe=_vendor(enabled=False),
        prospeo=prospeo,
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
        need="both",
        max_tier="prospeo",
        write_supabase=True,
        parallel=False,
    )
    assert out["phones_found"] == 1
    assert out["spend"] == _usd("aiark", 1.0) + _usd("prospeo", 10.0)


def test_estimate_stays_worst_case_email_only(monkeypatch) -> None:
    out = waterfall.enrich_waterfall(
        [
            {
                "domain": "roofco.com",
                "first_name": "Jane",
                "last_name": "Smith",
            }
        ],
        client_tag="peterson",
        need="email",
        max_tier="aiark",
        estimate_only=True,
        write_supabase=False,
    )
    assert out["estimate"]["aiark"]["credits_per_row"] == 1.5
    assert out["estimate"]["aiark"]["usd_est"] == _usd("aiark", 1.5)
