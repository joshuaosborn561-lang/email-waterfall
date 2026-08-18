"""Parallel executor timing and parity vs serial path."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from email_waterfall import waterfall
from email_waterfall.vendors.base import EmailHit


def _slow_vendor(*, latency: float = 0.2, email: str = "found@example.com"):
    m = MagicMock()
    m.enabled = True
    m.calls = 0
    m.hits = 0
    m.find_people.return_value = []

    def _find_email(first, last, domain, company_name="", **kwargs):
        m.calls += 1
        time.sleep(latency)
        m.hits += 1
        return EmailHit(
            email=f"{first.lower()}.{last.lower()}@{domain}",
            source_tier="getleads",
            status="valid",
        )

    m.find_email = _find_email
    m.find_email_bulk.return_value = []
    return m


def _patch_vendors(monkeypatch, gl) -> None:
    monkeypatch.setattr(waterfall, "GetLeadsClient", lambda: gl)
    monkeypatch.setattr(waterfall, "AiArkClient", lambda: _disabled())
    monkeypatch.setattr(waterfall, "LeadMagicClient", lambda: _disabled())
    monkeypatch.setattr(waterfall, "ProspeoClient", lambda: _disabled())
    monkeypatch.setattr(waterfall, "FullEnrichClient", lambda: _disabled())


def _disabled():
    m = MagicMock()
    m.enabled = False
    m.calls = 0
    m.hits = 0
    return m


def _rows(n: int) -> list[dict[str, str]]:
    return [
        {
            "domain": f"co{i}.example.com",
            "first_name": "Jane",
            "last_name": f"User{i}",
            "company_name": f"Company {i}",
        }
        for i in range(n)
    ]


def _parity_keys(result: dict) -> dict:
    return {
        "rows_in": result["rows_in"],
        "emails_found": result["emails_found"],
        "dms_found": result["dms_found"],
        "tier_stats": {
            tier: {
                "calls": stats.get("calls", 0),
                "email_hits": stats.get("email_hits", 0),
                "dm_hits": stats.get("dm_hits", 0),
            }
            for tier, stats in result.get("tier_stats", {}).items()
        },
    }


def test_parallel_twenty_companies_under_two_seconds_and_parity(monkeypatch) -> None:
    gl = _slow_vendor(latency=0.2)
    _patch_vendors(monkeypatch, gl)

    monkeypatch.setenv("COMPANY_CONCURRENCY", "20")
    monkeypatch.setenv("GETLEADS_CONCURRENCY", "10")

    rows = _rows(20)

    serial = waterfall.enrich_waterfall(
        rows,
        client_tag="peterson",
        need="email",
        write_supabase=False,
        parallel=False,
    )

    start = time.monotonic()
    parallel = waterfall.enrich_waterfall(
        rows,
        client_tag="peterson",
        need="email",
        write_supabase=False,
        parallel=True,
    )
    elapsed = time.monotonic() - start

    assert elapsed < 2.0
    assert _parity_keys(serial) == _parity_keys(parallel)
    assert parallel["emails_found"] == 20
    assert parallel["companies_done"] == 20
    assert parallel["companies_total"] == 20
    assert parallel["tier_stats"]["getleads"]["calls"] == 20
