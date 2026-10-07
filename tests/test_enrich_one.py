"""One-person lookup for ReplyHandler Slack cards."""

from __future__ import annotations

import inspect

from email_waterfall import waterfall
from email_waterfall.vendors.base import EmailHit, PhoneHit
from tests.test_waterfall import _patch_clients, _vendor


def test_enrich_one_person_returns_compact_hit(monkeypatch) -> None:
    ark = _vendor(email=EmailHit(email="jane@roofco.com", source_tier="aiark"))
    ark.find_mobile.return_value = PhoneHit(
        phone="+12015550100", source_tier="aiark"
    )
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=ark,
        lm=_vendor(enabled=True),
        fe=_vendor(enabled=True),
    )

    hit = waterfall.enrich_one_person(
        client_tag="replyhandler",
        email="jane@roofco.com",
        full_name="Jane Smith",
        need="both",
        max_tier="fullenrich",
        write_supabase=False,
    )
    assert hit["ok"] is True
    assert hit["email"] == "jane@roofco.com"
    assert hit["phone"] == "+12015550100"
    assert hit["phone_tier"] == "aiark"
    assert hit["max_tier"] == "fullenrich"
    assert hit["need"] == "both"
    assert hit["client_tag"] == "replyhandler"
    assert hit["sources"]["phone"] == "aiark"
    assert "raw" not in hit
    ark.find_mobile.assert_called()
    assert hit["website"] == "https://roofco.com"


def test_enrich_one_falls_through_to_fullenrich_phone(monkeypatch) -> None:
    fe = _vendor(enabled=True)
    fe.find_email.return_value = None
    fe.find_mobile.return_value = PhoneHit(
        phone="+19725550100", source_tier="fullenrich"
    )
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=_vendor(enabled=True),
        lm=_vendor(enabled=True),
        fe=fe,
        prospeo=_vendor(enabled=True),
    )

    hit = waterfall.enrich_one_person(
        client_tag="replyhandler",
        first_name="Jane",
        last_name="Smith",
        domain="roofco.com",
        need="both",
        max_tier="fullenrich",
        write_supabase=False,
    )
    assert hit["ok"] is True
    assert hit["phone"] == "+19725550100"
    assert hit["phone_tier"] == "fullenrich"
    fe.find_mobile.assert_called()


def test_enrich_one_requires_domain_or_name_company() -> None:
    hit = waterfall.enrich_one_person(
        client_tag="replyhandler",
        full_name="Just A Name",
        write_supabase=False,
    )
    assert hit["ok"] is False
    assert hit["reason"] == "need_domain_or_name_company"


def test_enrich_one_defaults_do_not_write_supabase(monkeypatch) -> None:
    writes: list[str] = []

    def boom(*_a, **_k):
        writes.append("write")
        raise AssertionError("Slack lookup must not write wf_* rows")

    monkeypatch.setattr(waterfall, "_write_company_contact", boom)
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=_vendor(enabled=False),
        lm=_vendor(enabled=False),
        fe=_vendor(enabled=False),
    )
    hit = waterfall.enrich_one_person(
        client_tag="replyhandler",
        email="jane@roofco.com",
        full_name="Jane Smith",
    )
    assert hit["ok"] is True
    assert writes == []


def test_mcp_exposes_enrich_person() -> None:
    import asyncio

    from mcp_server.server import enrich_person, mcp

    tools = mcp.list_tools()
    if inspect.iscoroutine(tools):
        tools = asyncio.run(tools)
    names = {t.name for t in tools}
    assert "enrich_person" in names
    params = inspect.signature(enrich_person).parameters
    assert params["write_supabase"].default is False
    assert params["max_tier"].default == "fullenrich"
    assert params["need"].default == "both"
    assert params["client_tag"].default is inspect.Parameter.empty
