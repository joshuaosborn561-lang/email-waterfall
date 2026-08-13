"""Waterfall: max_tier, client isolation, title filter, no Apify."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from email_waterfall import waterfall
from email_waterfall.vendors.base import EmailHit, PersonHit


def _vendor(*, enabled: bool = True, people=None, email=None):
    m = MagicMock()
    m.enabled = enabled
    m.calls = 0
    m.hits = 0
    m.find_people.return_value = people or []
    m.find_email.return_value = email
    m.find_email_bulk.return_value = []
    return m


def _patch_clients(monkeypatch, *, gl, ark, lm, fe) -> None:
    monkeypatch.setattr(waterfall, "GetLeadsClient", lambda: gl)
    monkeypatch.setattr(waterfall, "AiArkClient", lambda: ark)
    monkeypatch.setattr(waterfall, "LeadMagicClient", lambda: lm)
    monkeypatch.setattr(waterfall, "FullEnrichClient", lambda: fe)


def _patch_writes(monkeypatch, sink: dict) -> None:
    def companies(client, rows):
        sink["client"] = client.tag
        sink["companies_table"] = client.companies_table
        sink["companies"] = rows
        return len(rows)

    def contacts(client, rows):
        sink.setdefault("contacts", [])
        sink["contacts"].extend(rows)
        sink["contacts_table"] = client.contacts_table
        return len(rows)

    monkeypatch.setattr(waterfall.supabase_sync, "upsert_companies", companies)
    monkeypatch.setattr(
        waterfall.supabase_sync, "insert_contacts_ignore_conflict", contacts
    )
    monkeypatch.setattr(waterfall.supabase_sync, "insert_contacts", contacts)


def test_client_tag_required_on_enrich() -> None:
    with pytest.raises(ValueError, match="client_tag"):
        waterfall.enrich_waterfall(
            [{"domain": "x.com"}],
            client_tag="",
            write_supabase=False,
        )


def test_max_tier_blocks_fullenrich(monkeypatch) -> None:
    sink: dict = {}
    gl = _vendor(
        email=None,
        people=[
            PersonHit(
                first_name="Jane",
                last_name="Smith",
                title="Owner",
                source_tier="getleads",
            )
        ],
    )
    fe = _vendor(enabled=True)
    fe.find_email_bulk.return_value = [
        EmailHit(email="x@y.com", source_tier="fullenrich")
    ]
    _patch_clients(
        monkeypatch,
        gl=gl,
        ark=_vendor(enabled=False),
        lm=_vendor(enabled=True, email=None),
        fe=fe,
    )
    _patch_writes(monkeypatch, sink)

    out = waterfall.enrich_waterfall(
        [
            {
                "domain": "roofco.com",
                "first_name": "Jane",
                "last_name": "Smith",
                "company_name": "Roof Co",
                "title": "Owner",
            }
        ],
        client_tag="peterson",
        need="email",
        max_tier="leadmagic",
        write_supabase=True,
    )
    assert out["max_tier"] == "leadmagic"
    assert out["client_tag"] == "peterson"
    assert out["contacts_table"] == "peterson_contacts"
    fe.find_email_bulk.assert_not_called()
    fe.find_email.assert_not_called()
    assert out["vendors_enabled"]["fullenrich"] is False


def test_fullenrich_runs_when_max_tier_allows(monkeypatch) -> None:
    sink: dict = {}
    gl = _vendor(email=None)
    lm = _vendor(email=None)
    fe = _vendor(enabled=True)
    fe.find_email_bulk.return_value = [
        EmailHit(email="jane@roofco.com", source_tier="fullenrich", status="found")
    ]
    _patch_clients(monkeypatch, gl=gl, ark=_vendor(enabled=False), lm=lm, fe=fe)
    _patch_writes(monkeypatch, sink)

    out = waterfall.enrich_waterfall(
        [
            {
                "domain": "roofco.com",
                "first_name": "Jane",
                "last_name": "Smith",
                "title": "Owner",
            }
        ],
        client_tag="peterson",
        need="email",
        max_tier="fullenrich",
        write_supabase=True,
    )
    fe.find_email_bulk.assert_called_once()
    assert out["emails_found"] == 1
    assert sink["companies"][0]["email_source_tier"] == "fullenrich"


def test_stops_email_at_getleads(monkeypatch) -> None:
    sink: dict = {}
    gl = _vendor(
        email=EmailHit(email="jane@acme.test", source_tier="getleads", status="valid")
    )
    lm = _vendor(
        email=EmailHit(email="should-not@x.com", source_tier="leadmagic")
    )
    _patch_clients(
        monkeypatch, gl=gl, ark=_vendor(enabled=False), lm=lm, fe=_vendor(enabled=False)
    )
    _patch_writes(monkeypatch, sink)

    out = waterfall.enrich_waterfall(
        [
            {
                "domain": "acme.test",
                "first_name": "Jane",
                "last_name": "Smith",
                "company_name": "Acme",
                "title": "Owner",
            }
        ],
        client_tag="peterson",
        need="email",
        write_supabase=True,
    )
    assert out["emails_found"] == 1
    lm.find_email.assert_not_called()
    assert "csv" not in out and "items" not in out
    assert sink["companies_table"] == "peterson_companies"


def test_basco_title_match_skips_sales_manager(monkeypatch) -> None:
    sink: dict = {}
    ark = _vendor(
        people=[
            PersonHit(
                first_name="Sam",
                last_name="Seller",
                title="Sales Manager",
                source_tier="aiark",
            )
        ]
    )
    gl = _vendor(
        people=[
            PersonHit(
                first_name="Pat",
                last_name="Director",
                title="Service Director",
                source_tier="getleads",
            )
        ]
    )
    _patch_clients(
        monkeypatch, gl=gl, ark=ark, lm=_vendor(enabled=False), fe=_vendor(enabled=False)
    )
    _patch_writes(monkeypatch, sink)

    out = waterfall.enrich_waterfall(
        [{"domain": "paragonhonda.com", "company_name": "Paragon Honda"}],
        client_tag="basco",
        need="dm",
        require_title_match=True,
        write_supabase=True,
    )
    assert out["dms_found"] == 1
    assert out["client_tag"] == "basco"
    assert sink["contacts"][0]["first_name"] == "Pat"
    assert sink["contacts"][0]["client_tag"] == "basco"
    assert sink["companies_table"] == "basco_companies"
    ark.find_people.assert_not_called()


def test_getleads_runs_before_aiark_for_dm(monkeypatch) -> None:
    sink: dict = {}
    gl = _vendor(
        people=[
            PersonHit(
                first_name="Pat",
                last_name="Director",
                title="Service Director",
                source_tier="getleads",
            )
        ]
    )
    ark = _vendor(
        people=[
            PersonHit(
                first_name="Sam",
                last_name="Owner",
                title="Owner",
                source_tier="aiark",
            )
        ]
    )
    _patch_clients(
        monkeypatch, gl=gl, ark=ark, lm=_vendor(enabled=False), fe=_vendor(enabled=False)
    )
    _patch_writes(monkeypatch, sink)

    waterfall.enrich_waterfall(
        [{"domain": "paragonhonda.com", "company_name": "Paragon Honda"}],
        client_tag="basco",
        need="dm",
        require_title_match=True,
        write_supabase=True,
    )
    gl.find_people.assert_called()
    ark.find_people.assert_not_called()
    assert sink["contacts"][0]["source_tier"] == "getleads"


def test_max_tier_getleads_blocks_aiark(monkeypatch) -> None:
    sink: dict = {}
    gl = _vendor(people=[])
    ark = _vendor(
        people=[
            PersonHit(
                first_name="Pat",
                last_name="Director",
                title="Service Director",
                source_tier="aiark",
            )
        ]
    )
    _patch_clients(
        monkeypatch, gl=gl, ark=ark, lm=_vendor(enabled=True), fe=_vendor(enabled=False)
    )
    _patch_writes(monkeypatch, sink)

    out = waterfall.enrich_waterfall(
        [{"domain": "paragonhonda.com"}],
        client_tag="basco",
        need="dm",
        max_tier="getleads",
        write_supabase=True,
    )
    ark.find_people.assert_not_called()
    assert out["vendors_enabled"]["aiark"] is False
    assert out["max_tier"] == "getleads"


def test_duplicate_domain_rows_deduped_before_upsert(monkeypatch) -> None:
    sink: dict = {}
    gl = _vendor(
        email=EmailHit(email="a@paragonhonda.com", source_tier="getleads")
    )
    _patch_clients(
        monkeypatch, gl=gl, ark=_vendor(enabled=False), lm=_vendor(enabled=False), fe=_vendor(enabled=False)
    )
    _patch_writes(monkeypatch, sink)

    out = waterfall.enrich_waterfall(
        [
            {
                "domain": "paragonhonda.com",
                "first_name": "Pat",
                "last_name": "Lee",
                "title": "Service Director",
            },
            {
                "domain": "www.paragonhonda.com",
                "first_name": "Pat",
                "last_name": "Lee",
                "title": "Service Director",
                "company_name": "Paragon Honda",
            },
        ],
        client_tag="basco",
        need="email",
        write_supabase=True,
    )
    assert out["rows_in"] == 2
    # upsert_companies receives already-built rows; our fake does not dedupe,
    # but the real upsert_companies does. Call the real dedupe via the module.
    from email_waterfall.supabase_sync import dedupe_companies

    merged = dedupe_companies(sink["companies"])
    assert len(merged) == 1
    assert merged[0]["domain"] == "paragonhonda.com"


def test_apify_is_not_a_real_tier() -> None:
    assert "apify" not in waterfall.TIER_ORDER
    assert waterfall.normalize_max_tier("apify") == "getleads"
    assert waterfall.TIER_ORDER[0] == "getleads"
    assert waterfall.TIER_ORDER[1] == "aiark"


def test_empty_rows() -> None:
    out = waterfall.enrich_waterfall([], client_tag="peterson", write_supabase=False)
    assert out["rows_in"] == 0
    assert out["companies_table"] == "peterson_companies"
