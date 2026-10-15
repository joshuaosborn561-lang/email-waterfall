"""Row claiming: SKIP LOCKED leases, restart resume, ceiling release."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from email_waterfall import source as table_source
from email_waterfall import waterfall
from email_waterfall.concurrency import MAX_COMPANY_CONCURRENCY, company_concurrency
from email_waterfall.vendors.base import EmailHit


def _src() -> table_source.TableSource:
    src = table_source.parse_source(
        {"table": "client_peterson.email_resolution", "where": "wf_status is null"}
    )
    src.writeback = True
    return src


def test_company_concurrency_caps_at_fifty(monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_CONCURRENCY", "80")
    assert company_concurrency() == MAX_COMPANY_CONCURRENCY
    monkeypatch.setenv("COMPANY_CONCURRENCY", "20")
    assert company_concurrency() == 20
    monkeypatch.setenv("COMPANY_CONCURRENCY", "0")
    assert company_concurrency() == 1


def test_claim_source_rows_calls_rpc(monkeypatch) -> None:
    src = _src()
    calls: list[tuple[str, dict]] = []

    def fake_rpc(path, body, **kwargs):
        calls.append((path, body))
        return [
            {
                "id": 9,
                "first_name": "Jane",
                "last_name": "Owner",
                "company_name": "Org",
                "domain": "org.example",
            }
        ]

    monkeypatch.setattr(table_source, "discover_column_map", lambda s: None)
    monkeypatch.setattr(table_source, "resolve_credentials", lambda pid: ("https://x", "k"))
    monkeypatch.setattr(table_source, "_rpc_json", fake_rpc)
    rows = table_source.claim_source_rows(src, owner="ew:1:abc", n=1)
    assert calls[0][0] == "rpc/ew_claim_source"
    assert calls[0][1]["p_owner"] == "ew:1:abc"
    assert calls[0][1]["p_limit"] == 1
    assert calls[0][1]["p_lease_seconds"] == table_source.LEASE_SECONDS_DEFAULT
    assert table_source.LEASE_SECONDS_DEFAULT == 900
    assert rows[0]["_source_key"] == 9
    assert rows[0]["first_name"] == "Jane"


def test_claim_source_missing_rpc_raises(monkeypatch) -> None:
    src = _src()

    def fake_rpc(path, body, **kwargs):
        raise RuntimeError("Could not find the function public.ew_claim_source in the schema cache")

    monkeypatch.setattr(table_source, "discover_column_map", lambda s: None)
    monkeypatch.setattr(table_source, "resolve_credentials", lambda pid: ("https://x", "k"))
    monkeypatch.setattr(table_source, "_rpc_json", fake_rpc)
    with pytest.raises(table_source.ClaimUnavailable):
        table_source.claim_source_rows(src, owner="ew:1:abc")


def test_release_claims_sends_owner_and_keys(monkeypatch) -> None:
    src = _src()
    calls: list[tuple[str, dict]] = []

    def fake_rpc(path, body, **kwargs):
        calls.append((path, body))
        return 2

    monkeypatch.setattr(table_source, "resolve_credentials", lambda pid: ("https://x", "k"))
    monkeypatch.setattr(table_source, "_rpc_json", fake_rpc)
    n = table_source.release_claims(src, owner="ew:1:abc", keys=[1, 2])
    assert n == 2
    assert calls[0][0] == "rpc/ew_release_claims"
    assert calls[0][1]["p_owner"] == "ew:1:abc"
    assert calls[0][1]["p_keys"] == ["1", "2"]


def _disabled():
    from unittest.mock import MagicMock

    m = MagicMock()
    m.enabled = False
    m.calls = 0
    m.hits = 0
    m.find_mobile.return_value = None
    return m


def _gl_hit():
    from unittest.mock import MagicMock

    m = MagicMock()
    m.enabled = True
    m.calls = 0
    m.hits = 0
    m.find_people.return_value = []

    def _find_email(first, last, domain, company_name="", **kwargs):
        m.calls += 1
        m.hits += 1
        return EmailHit(
            email=f"{first.lower()}.{last.lower()}@{domain}",
            source_tier="getleads",
            status="valid",
        )

    m.find_email = _find_email
    m.find_email_bulk.return_value = []
    return m


def test_claimed_run_does_not_reclaim_same_key(monkeypatch) -> None:
    queue = [
        {
            "id": i,
            "first_name": "Jane",
            "last_name": f"User{i}",
            "company_name": "Org",
            "domain": f"co{i}.example.com",
        }
        for i in range(8)
    ]
    lock = threading.Lock()
    seen: list[int] = []

    def fake_claim(src, *, owner, n=1, lease_seconds=900):
        with lock:
            if not queue:
                return []
            raw = queue.pop(0)
            seen.append(raw["id"])
            return [
                {
                    "_source_key": raw["id"],
                    "first_name": raw["first_name"],
                    "last_name": raw["last_name"],
                    "company_name": raw["company_name"],
                    "domain": raw["domain"],
                }
            ]

    released: list[str] = []

    def fake_release(src, *, owner, keys=None):
        released.append(owner)
        return 0

    monkeypatch.setattr(waterfall, "GetLeadsClient", lambda: _gl_hit())
    monkeypatch.setattr(waterfall, "SmartleadClient", lambda: _disabled())
    monkeypatch.setattr(waterfall, "AiArkClient", lambda: _disabled())
    monkeypatch.setattr(waterfall, "ProspeoClient", lambda: _disabled())
    monkeypatch.setattr(waterfall, "FullEnrichClient", lambda: _disabled())
    monkeypatch.setattr(waterfall, "VeriphoneClient", lambda: _disabled())
    monkeypatch.setattr(table_source, "fetch_source_rows", lambda src: [
        {
            "_source_key": r["id"],
            "first_name": r["first_name"],
            "last_name": r["last_name"],
            "company_name": r["company_name"],
            "domain": r["domain"],
        }
        for r in [
            {
                "id": i,
                "first_name": "Jane",
                "last_name": f"User{i}",
                "company_name": "Org",
                "domain": f"co{i}.example.com",
            }
            for i in range(8)
        ]
    ])
    monkeypatch.setattr(table_source, "ensure_writeback_columns", lambda src: [])
    monkeypatch.setattr(table_source, "claim_source_rows", fake_claim)
    monkeypatch.setattr(table_source, "release_claims", fake_release)
    monkeypatch.setattr(table_source, "writeback_result", lambda *a, **k: None)
    monkeypatch.setenv("COMPANY_CONCURRENCY", "4")

    out = waterfall.enrich_waterfall(
        client_tag="peterson",
        need="email",
        source_table="client_peterson.email_resolution",
        where="wf_status is null",
        write_supabase=False,
        writeback=True,
        approve_cost_usd=50,
        estimate_only=False,
    )
    assert out["emails_found"] == 8
    assert out["companies_done"] == 8
    assert sorted(seen) == list(range(8))
    assert len(seen) == len(set(seen))
    assert released  # leftover lease cleared for this owner


def test_ceiling_releases_unprocessed_claims(monkeypatch) -> None:
    """A ceiling stop must not stamp not_found on rows that were never tried."""
    from email_waterfall._wf import engine as eng

    inventory = [
        {
            "_source_key": 1,
            "first_name": "Jane",
            "last_name": "One",
            "company_name": "Org",
            "domain": "one.example.com",
        },
        {
            "_source_key": 2,
            "first_name": "Jane",
            "last_name": "Two",
            "company_name": "Org",
            "domain": "two.example.com",
        },
    ]
    lock = threading.Lock()
    claimed_q = list(inventory)

    def fake_claim(src, *, owner, n=1, lease_seconds=900):
        with lock:
            if not claimed_q:
                return []
            return [claimed_q.pop(0)]

    release_calls: list[dict] = []

    def fake_release(src, *, owner, keys=None):
        release_calls.append({"owner": owner, "keys": list(keys or [])})
        return 0

    statuses: list[str] = []

    def fake_writeback(src, **kwargs):
        statuses.append(kwargs.get("status") or "")

    monkeypatch.setattr(waterfall, "GetLeadsClient", lambda: _gl_hit())
    monkeypatch.setattr(waterfall, "SmartleadClient", lambda: _disabled())
    monkeypatch.setattr(waterfall, "AiArkClient", lambda: _disabled())
    monkeypatch.setattr(waterfall, "ProspeoClient", lambda: _disabled())
    monkeypatch.setattr(waterfall, "FullEnrichClient", lambda: _disabled())
    monkeypatch.setattr(waterfall, "VeriphoneClient", lambda: _disabled())
    monkeypatch.setattr(table_source, "fetch_source_rows", lambda src: list(inventory))
    monkeypatch.setattr(table_source, "ensure_writeback_columns", lambda src: [])
    monkeypatch.setattr(table_source, "claim_source_rows", fake_claim)
    monkeypatch.setattr(table_source, "release_claims", fake_release)
    monkeypatch.setattr(table_source, "writeback_result", fake_writeback)
    monkeypatch.setenv("COMPANY_CONCURRENCY", "1")

    original_bump = eng.Waterfall._bump_attempt

    def bump(self, tier, row, *, worst_credits=None):
        ok = original_bump(self, tier, row, worst_credits=worst_credits)
        self.stopped_at_ceiling = True
        return ok

    monkeypatch.setattr(eng.Waterfall, "_bump_attempt", bump)

    out = waterfall.enrich_waterfall(
        client_tag="peterson",
        need="email",
        source_table="client_peterson.email_resolution",
        where="wf_status is null",
        write_supabase=False,
        writeback=True,
        approve_cost_usd=50,
    )
    assert out["status"] == "stopped_at_ceiling"
    assert out["companies_done"] == 1
    assert statuses.count("not_found") == 0
    assert any(c["owner"].startswith("ew:") for c in release_calls)


def test_missing_claim_rpc_falls_back_to_fetch_all(monkeypatch) -> None:
    rows = [
        {
            "_source_key": 1,
            "first_name": "Jane",
            "last_name": "User",
            "company_name": "Org",
            "domain": "org.example.com",
        }
    ]

    def boom(*a, **k):
        raise table_source.ClaimUnavailable("ew_claim_source missing")

    monkeypatch.setattr(waterfall, "GetLeadsClient", lambda: _gl_hit())
    monkeypatch.setattr(waterfall, "SmartleadClient", lambda: _disabled())
    monkeypatch.setattr(waterfall, "AiArkClient", lambda: _disabled())
    monkeypatch.setattr(waterfall, "ProspeoClient", lambda: _disabled())
    monkeypatch.setattr(waterfall, "FullEnrichClient", lambda: _disabled())
    monkeypatch.setattr(waterfall, "VeriphoneClient", lambda: _disabled())
    monkeypatch.setattr(table_source, "fetch_source_rows", lambda src: rows)
    monkeypatch.setattr(table_source, "ensure_writeback_columns", lambda src: [])
    monkeypatch.setattr(table_source, "claim_source_rows", boom)
    monkeypatch.setattr(table_source, "release_claims", lambda *a, **k: 0)
    monkeypatch.setattr(table_source, "writeback_result", lambda *a, **k: None)

    out = waterfall.enrich_waterfall(
        client_tag="peterson",
        need="email",
        source_table="client_peterson.email_resolution",
        where="wf_status is null",
        write_supabase=False,
        writeback=True,
        approve_cost_usd=50,
    )
    assert out["emails_found"] == 1
    assert out["companies_done"] == 1


def test_claim_rpc_default_is_fifteen_minute_lease() -> None:
    assert table_source.LEASE_SECONDS_DEFAULT == 900
    sql = Path("supabase/migrations/007_ew_claim_source.sql").read_text()
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "p_lease_seconds integer DEFAULT 900" in sql
    assert "wf_status = 'processing'" in sql
    assert "ew_release_claims" in sql
    assert "dl_status" not in sql
    assert "sg_exclude" not in sql
    assert "pdl" not in sql.lower()


def test_estimate_only_does_not_claim(monkeypatch) -> None:
    claimed = {"n": 0}

    def boom(*a, **k):
        claimed["n"] += 1
        raise AssertionError("estimate_only must not claim")

    monkeypatch.setattr(
        table_source,
        "fetch_source_rows",
        lambda src: [
            {
                "_source_key": 1,
                "first_name": "Jane",
                "last_name": "User",
                "company_name": "Org",
                "domain": "org.example.com",
            }
        ],
    )
    monkeypatch.setattr(table_source, "claim_source_rows", boom)
    out = waterfall.enrich_waterfall(
        client_tag="peterson",
        need="email",
        source_table="client_peterson.email_resolution",
        where="wf_status is null",
        estimate_only=True,
        writeback=True,
        write_supabase=False,
        approve_cost_usd=50,
    )
    assert out["estimate_only"] is True
    assert out["spend"] == 0
    assert claimed["n"] == 0


def test_writeback_never_includes_forbidden_columns(monkeypatch) -> None:
    src = table_source.parse_source({"table": "ew_names_ready"})
    src._present_writeback = set(table_source.ALL_WRITEBACK_COLUMNS)
    bodies: list[dict] = []

    def fake_request(method, path, **kwargs):
        bodies.append(kwargs.get("body") or {})
        return 200, ""

    monkeypatch.setattr(table_source, "resolve_credentials", lambda pid: ("https://x", "k"))
    monkeypatch.setattr(table_source.supabase_sync, "request_on", fake_request)
    table_source.writeback_result(
        src, key=1, status="found", email="a@b.example", vendor="getleads"
    )
    body = bodies[0]
    assert set(body) <= set(table_source.WRITEBACK_COLUMNS)
    assert "dl_status" not in body
    assert "sg_exclude" not in body
    assert not any(k.startswith("skip_") for k in body)
    assert body["wf_lease_until"] is None
    assert body["wf_lease_owner"] is None


def test_paid_vendors_skipped_when_free_tier_hits(monkeypatch) -> None:
    paid_calls = {"aiark": 0, "prospeo": 0, "fullenrich": 0}

    def paid(name: str):
        m = _disabled()
        m.enabled = True

        def _email(*a, **k):
            paid_calls[name] += 1
            return None

        m.find_email = _email
        return m

    claimed = [
        {
            "_source_key": 1,
            "first_name": "Jane",
            "last_name": "User",
            "company_name": "Org",
            "domain": "org.example.com",
        }
    ]

    monkeypatch.setattr(waterfall, "GetLeadsClient", lambda: _gl_hit())
    monkeypatch.setattr(waterfall, "SmartleadClient", lambda: _disabled())
    monkeypatch.setattr(waterfall, "AiArkClient", lambda: paid("aiark"))
    monkeypatch.setattr(waterfall, "ProspeoClient", lambda: paid("prospeo"))
    monkeypatch.setattr(waterfall, "FullEnrichClient", lambda: paid("fullenrich"))
    monkeypatch.setattr(waterfall, "VeriphoneClient", lambda: _disabled())
    monkeypatch.setattr(table_source, "fetch_source_rows", lambda src: list(claimed))
    monkeypatch.setattr(table_source, "ensure_writeback_columns", lambda src: [])
    monkeypatch.setattr(
        table_source,
        "claim_source_rows",
        lambda src, *, owner, n=1, lease_seconds=900: [claimed.pop(0)] if claimed else [],
    )
    monkeypatch.setattr(table_source, "release_claims", lambda *a, **k: 0)
    monkeypatch.setattr(table_source, "writeback_result", lambda *a, **k: None)
    monkeypatch.setenv("COMPANY_CONCURRENCY", "1")

    out = waterfall.enrich_waterfall(
        client_tag="peterson",
        need="email",
        source_table="client_peterson.email_resolution",
        where="wf_status is null",
        write_supabase=False,
        writeback=True,
        approve_cost_usd=50,
    )
    assert out["emails_found"] == 1
    assert paid_calls == {"aiark": 0, "prospeo": 0, "fullenrich": 0}


def test_vendor_modules_never_call_pdl() -> None:
    root = Path("email_waterfall")
    hits: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text().lower()
        if "pdl" in text or "peopledatalabs" in text or "people data labs" in text:
            hits.append(str(path))
    assert hits == []
