"""estimate_only: counts, tiers, credits — no enrichment vendor calls."""

from __future__ import annotations

from unittest.mock import MagicMock

from email_waterfall import waterfall
from tests.test_waterfall import _patch_clients, _patch_writes, _vendor


def _mute_smartlead(monkeypatch) -> MagicMock:
    sl = MagicMock()
    sl.enabled = False
    sl.api_key = ""
    sl.credit_snapshot.return_value = {}
    sl.find_email = MagicMock()
    sl.refresh_credits = MagicMock()
    monkeypatch.setattr(waterfall, "SmartleadClient", lambda timeout=8: sl)
    return sl


def test_estimate_only_name_company_no_spend(monkeypatch) -> None:
    gl = _vendor(enabled=True)
    ark = _vendor(enabled=True)
    lm = _vendor(enabled=True)
    fe = _vendor(enabled=True)
    _patch_clients(monkeypatch, gl=gl, ark=ark, lm=lm, fe=fe, prospeo=_vendor(enabled=True))
    _mute_smartlead(monkeypatch)
    _patch_writes(monkeypatch, {})

    rows = [
        {
            "first_name": "Jane",
            "last_name": "Doe",
            "company_name": "Helping Hands Inc",
        }
        for _ in range(956)
    ]
    out = waterfall.enrich_waterfall(
        rows,
        client_tag="peterson_earthworks",
        need="email",
        max_tier="fullenrich",
        estimate_only=True,
        write_supabase=True,
    )
    assert out["estimate_only"] is True
    assert out["rows_in"] == 956
    assert out["modes"]["name_company"] == 956
    assert "domain" not in out["modes"]
    assert out["tiers_by_mode"]["name_company"] == [
        "aiark",
        "leadmagic",
        "prospeo",
        "fullenrich",
    ]
    assert "getleads" not in out["estimate"]
    assert "smartlead" not in out["estimate"]
    assert out["estimate"]["aiark"]["rows"] == 956
    assert out["estimate"]["aiark"]["credits_est"] == 1434.0
    assert out["estimate"]["leadmagic"]["credits_est"] == 956.0
    assert out["estimate"]["prospeo"]["credits_est"] == 956.0
    assert out["estimate"]["fullenrich"]["credits_est"] == 956.0
    assert out["spend"] == 0
    assert out["client_tag"] == "peterson_earthworks"
    assert out["contacts_table"] == "peterson_earthworks_wf_contacts"
    gl.find_email.assert_not_called()
    ark.find_email.assert_not_called()
    lm.find_email.assert_not_called()
    fe.find_email.assert_not_called()
    fe.find_email_bulk.assert_not_called()
    assert "items" not in out and "csv" not in out


def test_estimate_only_source_does_not_call_vendors(monkeypatch) -> None:
    sl = _mute_smartlead(monkeypatch)
    fetched = [
        {
            "_source_key": i,
            "first_name": "A",
            "last_name": "B",
            "company_name": "Org",
        }
        for i in range(956)
    ]
    monkeypatch.setattr(waterfall.table_source, "fetch_source_rows", lambda src: fetched)
    monkeypatch.setattr(
        waterfall.table_source, "ensure_writeback_columns", lambda src: (_ for _ in ()).throw(
            AssertionError("writeback must not run on estimate")
        ),
    )
    out = waterfall.enrich_waterfall(
        client_tag="peterson_earthworks",
        need="email",
        max_tier="fullenrich",
        estimate_only=True,
        writeback=True,
        source={
            "project_id": "kemvxzhcxvynmoutwdrh",
            "table": "ew_names_ready",
            "where": "wf_status is null",
            "map": {
                "first_name": "first_name",
                "last_name": "last_name",
                "company_name": "company_name",
            },
        },
    )
    assert out["rows_in"] == 956
    assert out["modes"]["name_company"] == 956
    assert out["spend"] == 0
    sl.find_email.assert_not_called()


def test_estimate_only_domain_rows_include_early_tiers(monkeypatch) -> None:
    _mute_smartlead(monkeypatch)
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
        max_tier="leadmagic",
        estimate_only=True,
        write_supabase=False,
    )
    assert out["modes"]["domain"] == 1
    assert out["tiers_by_mode"]["domain"] == [
        "getleads",
        "smartlead",
        "aiark",
        "leadmagic",
    ]
    assert out["estimate"]["getleads"]["rows"] == 1
    assert out["spend"] == 0
