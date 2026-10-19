"""Shared gates: company workers, Supabase pool, GetLeads rate, Smartlead slots."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from email_waterfall.concurrency import (
    DEFAULT_COMPANY_CONCURRENCY,
    DEFAULT_SUPABASE_POOL,
    GETLEADS_MAX_RPS,
    MAX_COMPANY_CONCURRENCY,
    MAX_SUPABASE_POOL,
    RateLimiter,
    SharedSlotPool,
    VendorGate,
    company_concurrency,
    supabase_pool_size,
    vendor_concurrency,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += float(seconds)


def test_company_concurrency_default_is_twenty(monkeypatch) -> None:
    monkeypatch.delenv("COMPANY_CONCURRENCY", raising=False)
    assert DEFAULT_COMPANY_CONCURRENCY == 20
    assert company_concurrency() == 20


def test_company_concurrency_caps_at_fifty(monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_CONCURRENCY", "80")
    assert company_concurrency() == MAX_COMPANY_CONCURRENCY
    monkeypatch.setenv("COMPANY_CONCURRENCY", "20")
    assert company_concurrency() == 20
    monkeypatch.setenv("COMPANY_CONCURRENCY", "0")
    assert company_concurrency() == 1


def test_supabase_pool_default_is_eight_and_caps(monkeypatch) -> None:
    monkeypatch.delenv("SUPABASE_POOL_SIZE", raising=False)
    assert DEFAULT_SUPABASE_POOL == 8
    assert MAX_SUPABASE_POOL == 8
    assert supabase_pool_size() == 8
    monkeypatch.setenv("SUPABASE_POOL_SIZE", "32")
    assert supabase_pool_size() == 8
    monkeypatch.setenv("SUPABASE_POOL_SIZE", "3")
    assert supabase_pool_size() == 3


def test_shared_slot_pool_never_exceeds_eight() -> None:
    pool = SharedSlotPool(size=32)
    assert pool.size == 8
    overlap = {"n": 0, "peak": 0}
    lock = threading.Lock()

    def worker() -> None:
        with pool.acquire():
            with lock:
                overlap["n"] += 1
                overlap["peak"] = max(overlap["peak"], overlap["n"])
            time.sleep(0.04)
            with lock:
                overlap["n"] -= 1

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlap["peak"] <= 8
    assert pool.peak <= 8
    assert overlap["n"] == 0


def test_request_on_uses_shared_pool(monkeypatch) -> None:
    from email_waterfall import supabase_sync
    from email_waterfall.concurrency import supabase_pool

    supabase_pool.reset(8)
    overlap = {"n": 0, "peak": 0}
    lock = threading.Lock()

    class _Resp:
        status = 200

        def read(self) -> bytes:
            return b"[]"

        def __enter__(self) -> "_Resp":
            with lock:
                overlap["n"] += 1
                overlap["peak"] = max(overlap["peak"], overlap["n"])
            time.sleep(0.03)
            return self

        def __exit__(self, *_exc: object) -> bool:
            with lock:
                overlap["n"] -= 1
            return False

    monkeypatch.setattr(supabase_sync.request, "urlopen", lambda *_a, **_k: _Resp())

    def worker() -> None:
        supabase_sync.request_on("GET", "probe", url="https://example.test", key="k")

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlap["peak"] <= 8
    assert supabase_pool.peak <= 8
    supabase_pool.reset()


def test_worker_modules_do_not_open_libpq() -> None:
    banned = ("psycopg", "psycopg2", "asyncpg", "sqlalchemy", "pg8000")
    files = (
        Path("email_waterfall/supabase_sync.py"),
        Path("email_waterfall/source.py"),
        Path("email_waterfall/_wf/exec.py"),
        Path("email_waterfall/concurrency.py"),
    )
    for path in files:
        text = path.read_text()
        for name in banned:
            assert name not in text, f"{path} must not import {name}"


def test_rate_limiter_is_four_calls_per_second() -> None:
    clock = _Clock()
    limiter = RateLimiter(GETLEADS_MAX_RPS, sleep=clock.sleep, clock=clock)
    starts: list[float] = []
    for _ in range(5):
        limiter.wait()
        starts.append(clock.t)
    assert starts == [0.0, 0.25, 0.50, 0.75, 1.0]


def test_getleads_rate_limit_sits_on_top_of_in_flight_semaphore(monkeypatch) -> None:
    monkeypatch.setenv("GETLEADS_CONCURRENCY", "4")
    clock = _Clock()
    limiter = RateLimiter(4, sleep=clock.sleep, clock=clock)
    gate = VendorGate()
    starts: list[float] = []
    for _ in range(8):
        limiter.wait()
        with gate.acquire("getleads"):
            starts.append(clock.t)
    assert starts[0] == 0.0
    assert starts[-1] == 1.75
    gaps = [round(b - a, 5) for a, b in zip(starts, starts[1:])]
    assert all(gap >= 0.25 for gap in gaps)


def test_getleads_in_flight_semaphore_still_caps(monkeypatch) -> None:
    monkeypatch.setenv("GETLEADS_CONCURRENCY", "2")
    gate = VendorGate()
    overlap = {"n": 0, "peak": 0}
    lock = threading.Lock()

    def worker() -> None:
        with gate.acquire("getleads"):
            with lock:
                overlap["n"] += 1
                overlap["peak"] = max(overlap["peak"], overlap["n"])
            time.sleep(0.05)
            with lock:
                overlap["n"] -= 1

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlap["peak"] <= 2
    assert overlap["n"] == 0


def test_smartlead_default_limit_is_three(monkeypatch) -> None:
    monkeypatch.delenv("SMARTLEAD_CONCURRENCY", raising=False)
    assert vendor_concurrency("smartlead") == 3


def test_two_jobs_share_one_smartlead_slot(monkeypatch) -> None:
    monkeypatch.setenv("SMARTLEAD_CONCURRENCY", "1")
    monkeypatch.setenv("VENDOR_LOCK_DIR", "/tmp/ew-vendor-locks-test")
    gate = VendorGate()
    overlap = {"n": 0}
    lock = threading.Lock()

    def worker() -> None:
        with gate.acquire("smartlead"):
            with lock:
                overlap["n"] += 1
                current = overlap["n"]
            assert current == 1
            time.sleep(0.08)
            with lock:
                overlap["n"] -= 1

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlap["n"] == 0


def test_smartlead_clients_share_credit_state() -> None:
    from email_waterfall.vendors.smartlead import SmartleadClient, reset_shared_credits

    reset_shared_credits()
    a = SmartleadClient(api_key="one")
    b = SmartleadClient(api_key="two")
    a._credits_available = 10
    a._credits_total = 50000
    a._credits_used = 325
    a._mark_spent(1)
    assert b.credit_snapshot()["available"] == 9
    assert b.credit_snapshot()["used"] == 326
    reset_shared_credits()
