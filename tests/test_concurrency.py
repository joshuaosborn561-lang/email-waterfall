"""Shared Smartlead gate is process-wide, not per job."""

from __future__ import annotations

import threading
import time

from email_waterfall.concurrency import VendorGate, vendor_concurrency


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
