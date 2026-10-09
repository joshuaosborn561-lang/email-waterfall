"""Waterfall: max_tier, client isolation, title filter, no Apify."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from email_waterfall import waterfall
from email_waterfall.vendors.base import EmailHit, PersonHit, PhoneHit


def _vendor(*, enabled: bool = True, people=None, email=None):
    m = MagicMock()
    m.enabled = enabled
    m.calls = 0
    m.hits = 0
    m.find_people.return_value = people or []
    m.find_email.return_value = email
    m.find_email_bulk.return_value = []
    m.find_mobile.return_value = None
    return m


def _patch_clients(
    monkeypatch, *, gl, ark, fe, prospeo=None, smartlead=None, veriphone=None, lm=None
) -> None:
    monkeypatch.setattr(waterfall, "GetLeadsClient", lambda: gl)
    monkeypatch.setattr(
        waterfall,
        "SmartleadClient",
        lambda *args, **kwargs: smartlead or _vendor(enabled=False),
    )
    monkeypatch.setattr(waterfall, "AiArkClient", lambda: ark)
    monkeypatch.setattr(waterfall, "ProspeoClient", lambda: prospeo or _vendor(enabled=False))
    monkeypatch.setattr(waterfall, "FullEnrichClient", lambda: fe)
    monkeypatch.setattr(
        waterfall,
        "VeriphoneClient",
        lambda *args, **kwargs: veriphone or _vendor(enabled=False),
    )


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
    monkeypatch.setattr(waterfall.supabase_sync, "upsert_contacts", contacts)
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
    ark = _vendor(enabled=True, email=None)
    lm = _vendor(enabled=True, email=None)
    _patch_clients(monkeypatch, gl=gl, ark=ark, lm=lm, fe=fe)
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
    assert out["max_tier"] == "aiark"
    assert out["deprecated_max_tier"] == "leadmagic"
    assert out["client_tag"] == "peterson"
    assert out["contacts_table"] == "peterson_contacts"
    ark.find_email.assert_called()
    fe.find_email_bulk.assert_not_called()
    fe.find_email.assert_not_called()
    assert out["vendors_enabled"]["fullenrich"] is False
    assert out["vendors_enabled"]["prospeo"] is False


def test_fullenrich_runs_when_max_tier_allows(monkeypatch) -> None:
    sink: dict = {}
    gl = _vendor(email=None)
    lm = _vendor(email=None)
    fe = _vendor(enabled=True)
    fe_hit = EmailHit(email="jane@roofco.com", source_tier="fullenrich", status="found")
    fe.find_email.return_value = fe_hit
    fe.find_email_bulk.return_value = [fe_hit]
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
    fe.find_email.assert_called_once()
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
    ark = _vendor(enabled=True)
    _patch_clients(monkeypatch, gl=gl, ark=ark, lm=lm, fe=_vendor(enabled=False))
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
    ark.find_email.assert_not_called()
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
    assert "leadmagic" not in waterfall.TIER_ORDER
    assert waterfall.normalize_max_tier("apify") == "getleads"
    assert waterfall.TIER_ORDER == [
        "getleads",
        "smartlead",
        "aiark",
        "prospeo",
        "fullenrich",
    ]
    assert waterfall.DEFAULT_MAX_TIER == "prospeo"
    assert waterfall.normalize_max_tier("lm") == "aiark"
    assert waterfall.normalize_max_tier("leadmagic") == "aiark"
    assert waterfall.normalize_max_tier("lead_magic") == "aiark"
    assert waterfall.normalize_max_tier(None) == "prospeo"
    assert waterfall.normalize_max_tier("prospector") == "prospeo"
    assert waterfall.normalize_max_tier("fe") == "fullenrich"
    assert waterfall.normalize_max_tier("sl") == "smartlead"


def test_smartlead_runs_after_getleads_before_aiark(monkeypatch) -> None:
    sink: dict = {}
    gl = _vendor(email=None)
    sl = _vendor(
        email=EmailHit(email="jane@roofco.com", source_tier="smartlead", status="Valid")
    )
    ark = _vendor(
        email=EmailHit(email="should-not@x.com", source_tier="aiark")
    )
    _patch_clients(
        monkeypatch,
        gl=gl,
        ark=ark,
        lm=_vendor(enabled=False),
        fe=_vendor(enabled=False),
        smartlead=sl,
    )
    _patch_writes(monkeypatch, sink)

    out = waterfall.enrich_waterfall(
        [
            {
                "domain": "roofco.com",
                "first_name": "Jane",
                "last_name": "Smith",
                "company_name": "Roof Co",
            }
        ],
        client_tag="peterson",
        need="email",
        write_supabase=True,
    )
    assert out["emails_found"] == 1
    gl.find_email.assert_called()
    sl.find_email.assert_called()
    ark.find_email.assert_not_called()
    assert sink["companies"][0]["email_source_tier"] == "smartlead"
    assert out["vendors_enabled"]["smartlead"] is True


def test_smartlead_exhausted_falls_through_to_aiark(monkeypatch) -> None:
    sink: dict = {}
    sl = _vendor(enabled=False)
    ark = _vendor(
        email=EmailHit(email="jane@roofco.com", source_tier="aiark", status="found")
    )
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=ark,
        lm=_vendor(enabled=False),
        fe=_vendor(enabled=False),
        smartlead=sl,
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
        write_supabase=True,
    )
    sl.find_email.assert_not_called()
    ark.find_email.assert_called()
    assert out["emails_found"] == 1
    assert sink["companies"][0]["email_source_tier"] == "aiark"


def test_max_tier_getleads_blocks_smartlead(monkeypatch) -> None:
    sink: dict = {}
    sl = _vendor(
        email=EmailHit(email="jane@roofco.com", source_tier="smartlead")
    )
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=_vendor(enabled=False),
        lm=_vendor(enabled=False),
        fe=_vendor(enabled=False),
        smartlead=sl,
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
        max_tier="getleads",
        write_supabase=True,
    )
    sl.find_email.assert_not_called()
    assert out["vendors_enabled"]["smartlead"] is False
    assert out["max_tier"] == "getleads"


def test_aiark_is_second_email_tier(monkeypatch) -> None:
    sink: dict = {}
    gl = _vendor(email=None)
    ark = _vendor(
        email=EmailHit(email="pat@paragonhonda.com", source_tier="aiark", status="found")
    )
    prospeo = _vendor(email=EmailHit(email="should-not@x.com", source_tier="prospeo"))
    _patch_clients(
        monkeypatch, gl=gl, ark=ark, fe=_vendor(enabled=False), prospeo=prospeo
    )
    _patch_writes(monkeypatch, sink)

    out = waterfall.enrich_waterfall(
        [
            {
                "domain": "paragonhonda.com",
                "first_name": "Pat",
                "last_name": "Lee",
                "company_name": "Paragon Honda",
                "linkedin_url": "https://www.linkedin.com/in/pat-lee",
                "phone": "2015550100",
            }
        ],
        client_tag="basco",
        need="email",
        write_supabase=True,
    )
    assert out["emails_found"] == 1
    gl.find_email.assert_called()
    ark.find_email.assert_called()
    kwargs = ark.find_email.call_args
    assert kwargs.kwargs.get("linkedin_url") == "https://www.linkedin.com/in/pat-lee"
    assert kwargs.kwargs.get("phone") == "2015550100"
    prospeo.find_email.assert_not_called()
    assert sink["companies"][0]["email_source_tier"] == "aiark"


def test_aiark_email_from_linkedin_without_name(monkeypatch) -> None:
    sink: dict = {}
    gl = _vendor(email=None)
    ark = _vendor(
        email=EmailHit(email="pat@paragonhonda.com", source_tier="aiark", status="found")
    )
    lm = _vendor(email=None)
    _patch_clients(monkeypatch, gl=gl, ark=ark, lm=lm, fe=_vendor(enabled=False))
    _patch_writes(monkeypatch, sink)

    out = waterfall.enrich_waterfall(
        [
            {
                "domain": "paragonhonda.com",
                "linkedin_url": "https://www.linkedin.com/in/pat-lee",
            }
        ],
        client_tag="basco",
        need="email",
        write_supabase=True,
    )
    assert out["emails_found"] == 1
    gl.find_email.assert_called()
    ark.find_email.assert_called()


def test_aiark_email_uses_dm_linkedin(monkeypatch) -> None:
    sink: dict = {}
    gl = _vendor(
        email=None,
        people=[
            PersonHit(
                first_name="Pat",
                last_name="Director",
                title="Service Director",
                linkedin_url="https://www.linkedin.com/in/pat-director",
                source_tier="getleads",
                raw={"id": "ark-person-1"},
            )
        ],
    )
    ark = _vendor(
        email=EmailHit(email="pat@paragonhonda.com", source_tier="aiark", status="found")
    )
    _patch_clients(
        monkeypatch, gl=gl, ark=ark, lm=_vendor(enabled=False), fe=_vendor(enabled=False)
    )
    _patch_writes(monkeypatch, sink)

    out = waterfall.enrich_waterfall(
        [{"domain": "paragonhonda.com", "company_name": "Paragon Honda"}],
        client_tag="basco",
        need="both",
        write_supabase=True,
    )
    assert out["dms_found"] == 1
    assert out["emails_found"] == 1
    ark.find_email.assert_called()
    assert (
        ark.find_email.call_args.kwargs.get("linkedin_url")
        == "https://www.linkedin.com/in/pat-director"
    )


def test_prospeo_runs_after_aiark(monkeypatch) -> None:
    sink: dict = {}
    gl = _vendor(email=None)
    ark = _vendor(email=None)
    prospeo = _vendor(
        email=EmailHit(email="jane@roofco.com", source_tier="prospeo", status="VERIFIED")
    )
    fe = _vendor(enabled=True)
    fe.find_email_bulk.return_value = [
        EmailHit(email="should-not@x.com", source_tier="fullenrich")
    ]
    _patch_clients(monkeypatch, gl=gl, ark=ark, fe=fe, prospeo=prospeo)
    _patch_writes(monkeypatch, sink)

    out = waterfall.enrich_waterfall(
        [
            {
                "domain": "roofco.com",
                "first_name": "Jane",
                "last_name": "Smith",
                "company_name": "Roof Co",
            }
        ],
        client_tag="peterson",
        need="email",
        max_tier="fullenrich",
        write_supabase=True,
    )
    assert out["emails_found"] == 1
    ark.find_email.assert_called()
    prospeo.find_email.assert_called()
    fe.find_email_bulk.assert_not_called()
    assert sink["companies"][0]["email_source_tier"] == "prospeo"
    assert out["vendors_enabled"]["prospeo"] is True
    assert out["vendors_enabled"]["fullenrich"] is True


def test_max_tier_leadmagic_blocks_prospeo(monkeypatch) -> None:
    sink: dict = {}
    prospeo = _vendor(
        email=EmailHit(email="jane@roofco.com", source_tier="prospeo")
    )
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=_vendor(email=None),
        lm=_vendor(email=None),
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
        max_tier="leadmagic",
        write_supabase=True,
    )
    prospeo.find_email.assert_not_called()
    assert out["vendors_enabled"]["prospeo"] is False
    assert out["max_tier"] == "aiark"
    assert out["deprecated_max_tier"] == "leadmagic"
    assert any("leadmagic" in w for w in out["warnings"])


def test_default_max_tier_reaches_prospeo_not_fullenrich(monkeypatch) -> None:
    sink: dict = {}
    prospeo = _vendor(
        email=EmailHit(email="jane@roofco.com", source_tier="prospeo")
    )
    fe = _vendor(enabled=True)
    fe.find_email_bulk.return_value = [
        EmailHit(email="should-not@x.com", source_tier="fullenrich")
    ]
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=_vendor(email=None),
        fe=fe,
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
        write_supabase=True,
    )
    assert out["max_tier"] == "prospeo"
    prospeo.find_email.assert_called()
    fe.find_email.assert_not_called()
    fe.find_email_bulk.assert_not_called()
    assert out["vendors_enabled"]["prospeo"] is True
    assert out["vendors_enabled"]["fullenrich"] is False
    assert "leadmagic" not in out["vendors_enabled"]


def test_input_phone_writes_cellphone(monkeypatch) -> None:
    sink: dict = {}
    ark = _vendor(enabled=True)
    lm = _vendor(enabled=True)
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=ark,
        lm=lm,
        fe=_vendor(enabled=False),
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
        need="email",
        write_supabase=True,
    )
    assert out["phones_found"] == 1
    assert sink["contacts"][0]["cellphone"] == "201-555-0100"
    ark.find_mobile.assert_not_called()


def test_aiark_mobile_finder_writes_cellphone(monkeypatch) -> None:
    sink: dict = {}
    ark = _vendor(
        email=EmailHit(email="jane@roofco.com", source_tier="aiark"),
    )
    ark.find_mobile.return_value = PhoneHit(
        phone="+12015550100", source_tier="aiark"
    )
    lm = _vendor(enabled=True)
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=ark,
        lm=lm,
        fe=_vendor(enabled=False),
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
    ark.find_email.assert_called()
    ark.find_mobile.assert_called()


def test_prospeo_mobile_after_aiark_miss(monkeypatch) -> None:
    sink: dict = {}
    ark = _vendor(email=EmailHit(email="jane@roofco.com", source_tier="aiark"))
    ark.find_mobile.return_value = None
    prospeo = _vendor(enabled=True)
    prospeo.find_mobile.return_value = PhoneHit(
        phone="+19725550199", source_tier="prospeo"
    )
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
        write_supabase=True,
    )
    assert out["phones_found"] == 1
    assert sink["contacts"][0]["cellphone"] == "+19725550199"
    ark.find_mobile.assert_called()
    prospeo.find_mobile.assert_called()


def test_need_phone_resolves_dm_and_mobile_not_email(monkeypatch) -> None:
    sink: dict = {}
    gl = _vendor(
        people=[
            PersonHit(
                first_name="Jane",
                last_name="Smith",
                title="Owner",
                linkedin_url="https://www.linkedin.com/in/jane-smith",
                source_tier="getleads",
            )
        ]
    )
    ark = _vendor(email=EmailHit(email="jane@roofco.com", source_tier="aiark"))
    ark.find_mobile.return_value = PhoneHit(
        phone="+19725550111", source_tier="aiark"
    )
    _patch_clients(
        monkeypatch,
        gl=gl,
        ark=ark,
        lm=_vendor(enabled=False),
        fe=_vendor(enabled=False),
    )
    _patch_writes(monkeypatch, sink)

    out = waterfall.enrich_waterfall(
        [{"domain": "roofco.com", "company_name": "Roof Co"}],
        client_tag="peterson",
        need="phone",
        write_supabase=True,
    )
    assert out["need"] == "phone"
    assert out["dms_found"] == 1
    assert out["emails_found"] == 0
    assert out["phones_found"] == 1
    assert sink["contacts"][0]["cellphone"] == "+19725550111"
    ark.find_email.assert_not_called()
    ark.find_mobile.assert_called()


def test_empty_rows() -> None:
    out = waterfall.enrich_waterfall([], client_tag="peterson", write_supabase=False)
    assert out["rows_in"] == 0
    assert out["companies_table"] == "peterson_companies"


def test_legacy_skip_tiers_are_noop(monkeypatch) -> None:
    sink: dict = {}
    ark = _vendor(email=EmailHit(email="jane@roofco.com", source_tier="aiark"))
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=ark,
        fe=_vendor(enabled=False),
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
        skip_tiers=["leadmagic", "lm", "lead_magic"],
        write_supabase=True,
    )
    assert out["emails_found"] == 1
    assert out["skip_tiers"] == []
    assert out["deprecated_tiers"] == ["leadmagic", "lm", "lead_magic"]
    ark.find_email.assert_called()


def test_skip_tiers_skips_real_vendor(monkeypatch) -> None:
    sink: dict = {}
    ark = _vendor(email=EmailHit(email="jane@roofco.com", source_tier="aiark"))
    prospeo = _vendor(email=EmailHit(email="p@roofco.com", source_tier="prospeo"))
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
            }
        ],
        client_tag="peterson",
        need="email",
        skip_tiers="aiark",
        write_supabase=True,
    )
    ark.find_email.assert_not_called()
    prospeo.find_email.assert_called()
    assert out["skip_tiers"] == ["aiark"]
    assert sink["companies"][0]["email_source_tier"] == "prospeo"
