"""Bounded worker pool, error vs miss, costs, limit, cancel, stall."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest
import requests

from email_waterfall import waterfall
from email_waterfall.concurrency import (
    MAX_JOB_CONCURRENCY,
    WorkerSlots,
    job_concurrency,
    request_with_retry,
    reset_request_stats,
)
from email_waterfall.errors import VendorCallError
from email_waterfall.vendors.base import EmailHit
from tests.test_waterfall import _patch_clients, _patch_writes, _vendor


def test_job_concurrency_default_and_cap(monkeypatch) -> None:
    monkeypatch.delenv("TIER_CONCURRENCY", raising=False)
    monkeypatch.delenv("COMPANY_CONCURRENCY", raising=False)
    assert job_concurrency() == 12
    assert job_concurrency(99) == MAX_JOB_CONCURRENCY
    monkeypatch.setenv("TIER_CONCURRENCY", "8")
    assert job_concurrency() == 8


def test_limit_slices_snapshot_without_vendor_overrun(monkeypatch) -> None:
    sink: dict = {}
    gl = _vendor(email=EmailHit(email="a@x.com", source_tier="getleads"))
    _patch_clients(
        monkeypatch,
        gl=gl,
        ark=_vendor(enabled=False),
        lm=_vendor(enabled=False),
        fe=_vendor(enabled=False),
    )
    _patch_writes(monkeypatch, sink)
    rows = [
        {"domain": f"co{i}.com", "first_name": "A", "last_name": "B"}
        for i in range(50)
    ]
    out = waterfall.enrich_waterfall(
        rows,
        client_tag="peterson",
        need="email",
        write_supabase=False,
        limit=5,
        concurrency=2,
    )
    assert out["rows_fetched"] == 5
    assert out["rows_in"] == 5
    assert out["processed"] == 5
    assert gl.find_email.call_count == 5


def test_tier_breakdown_has_credits_and_cost(monkeypatch) -> None:
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=EmailHit(email="a@x.com", source_tier="getleads")),
        ark=_vendor(enabled=False),
        lm=_vendor(enabled=False),
        fe=_vendor(enabled=False),
    )
    _patch_writes(monkeypatch, {})
    out = waterfall.enrich_waterfall(
        [{"domain": "roofco.com", "first_name": "Jane", "last_name": "Smith"}],
        client_tag="peterson",
        need="email",
        write_supabase=False,
    )
    for name, row in out["tier_breakdown"].items():
        assert "cost_usd" in row
        assert "credits" in row
        assert "cost_note" in row
        if name == "smartlead":
            assert row["cost_note"] == "included plan allotment"


def test_vendor_call_error_on_exhausted_429(monkeypatch) -> None:
    reset_request_stats()

    def boom(*a, **k):
        r = MagicMock()
        r.status_code = 429
        r.headers = {}
        return r

    monkeypatch.setattr(requests, "request", boom)
    monkeypatch.setattr("email_waterfall.concurrency.time.sleep", lambda *_: None)
    with pytest.raises(VendorCallError) as exc:
        request_with_retry("getleads", "GET", "https://example.com", max_attempts=2)
    assert exc.value.kind == "throttle"


def test_errored_row_is_not_a_miss(monkeypatch) -> None:
    gl = _vendor(enabled=True)

    def boom(*a, **k):
        raise VendorCallError("getleads", "timeout", "down")

    gl.find_email.side_effect = boom
    _patch_clients(
        monkeypatch,
        gl=gl,
        ark=_vendor(enabled=False),
        lm=_vendor(enabled=False),
        fe=_vendor(enabled=False),
    )
    _patch_writes(monkeypatch, {})
    monkeypatch.setattr(waterfall.time, "sleep", lambda *_: None)
    writes: list = []
    monkeypatch.setattr(
        waterfall.table_source,
        "writeback_results_batch",
        lambda src, items, **k: writes.append(list(items)) or 0,
    )
    out = waterfall.enrich_waterfall(
        [{"domain": "roofco.com", "first_name": "Jane", "last_name": "Smith"}],
        client_tag="peterson",
        need="email",
        write_supabase=False,
        parallel=False,
    )
    assert out["errored"] == 1
    assert out["emails_found"] == 0
    assert out["none"] == 0
    assert out["accepted"] == 0
    flushed = [item for chunk in writes for item in chunk if not item.get("errored")]
    assert flushed == []


def test_slots_shrink_on_throttle() -> None:
    slots = WorkerSlots(4)
    assert slots.effective == 4
    slots.shrink(seconds=0.15)
    assert slots.effective == 3
    deadline = time.time() + 2
    while time.time() < deadline and slots.effective < 4:
        time.sleep(0.05)
    assert slots.effective == 4


def test_cancel_stops_workers(monkeypatch) -> None:
    ev = threading.Event()
    gl = _vendor(enabled=True)

    def slow(*a, **k):
        time.sleep(0.15)
        return EmailHit(email="a@x.com", source_tier="getleads")

    gl.find_email.side_effect = slow
    _patch_clients(
        monkeypatch,
        gl=gl,
        ark=_vendor(enabled=False),
        lm=_vendor(enabled=False),
        fe=_vendor(enabled=False),
    )
    _patch_writes(monkeypatch, {})
    rows = [
        {"domain": f"co{i}.com", "first_name": "A", "last_name": "B"}
        for i in range(40)
    ]

    def cancel_soon() -> None:
        time.sleep(0.12)
        ev.set()

    threading.Thread(target=cancel_soon, daemon=True).start()
    started = time.monotonic()
    out = waterfall.enrich_waterfall(
        rows,
        client_tag="peterson",
        need="email",
        write_supabase=False,
        concurrency=4,
        cancel_event=ev,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 8.0
    assert out["cancelled"] is True
    assert out["processed"] < 40


def test_writeback_batch_skips_errored(monkeypatch) -> None:
    from email_waterfall import source as srcmod

    called: list = []

    class Src:
        writeback = True

    def fake(src, **kwargs):
        called.append(kwargs)

    monkeypatch.setattr(srcmod, "writeback_result", fake)
    items = [
        {"row": {"_source_key": 1}, "email": "a@x.com", "errored": False},
        {"row": {"_source_key": 2}, "email": "", "errored": True},
    ]
    n = srcmod.writeback_results_batch(Src(), items, chunk_size=100)  # type: ignore[arg-type]
    assert n == 1
    assert called[0]["key"] == 1
