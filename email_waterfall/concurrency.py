"""Concurrency limits and retry helpers for vendor HTTP calls.

VendorGate is process-wide (all MCP background jobs share it) and, for
Smartlead, also cross-process via fcntl slot files so extra uvicorn workers
cannot stampede the finder.
"""

from __future__ import annotations

import fcntl
import os
import random
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import requests

TIER_ENV_KEYS: dict[str, str] = {
    "getleads": "GETLEADS_CONCURRENCY",
    "smartlead": "SMARTLEAD_CONCURRENCY",
    "aiark": "AIARK_CONCURRENCY",
    "leadmagic": "LEADMAGIC_CONCURRENCY",
    "prospeo": "PROSPEO_CONCURRENCY",
    "fullenrich": "FULLENRICH_CONCURRENCY",
}

DEFAULT_VENDOR_LIMITS: dict[str, int] = {
    "getleads": 10,
    "smartlead": 3,
    "aiark": 8,
    "leadmagic": 6,
    "prospeo": 6,
    "fullenrich": 4,
}

CROSS_PROCESS_TIERS = frozenset({"smartlead"})

DEFAULT_COMPANY_CONCURRENCY = 40


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def company_concurrency() -> int:
    return _env_int("COMPANY_CONCURRENCY", DEFAULT_COMPANY_CONCURRENCY)


def vendor_concurrency(tier: str) -> int:
    key = TIER_ENV_KEYS.get(tier)
    default = DEFAULT_VENDOR_LIMITS.get(tier, 4)
    return _env_int(key or "", default) if key else default


def _lock_dir() -> Path:
    raw = (os.environ.get("VENDOR_LOCK_DIR") or "").strip()
    if raw:
        path = Path(raw)
    else:
        path = Path(__file__).resolve().parent.parent / "data" / "locks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _acquire_slot(tier: str, limit: int) -> int | None:
    """Block until one of `limit` fcntl slots is free. None if flock is unavailable."""
    slots = _lock_dir() / f"{tier}.slots"
    # One file, N byte-range locks — shared across every job in every worker.
    fd = os.open(str(slots), os.O_CREAT | os.O_RDWR, 0o644)
    os.ftruncate(fd, max(limit, 1))
    while True:
        for i in range(max(limit, 1)):
            try:
                fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, i)
                os.lseek(fd, 0, os.SEEK_SET)
                # stash the locked offset in an unused fd flag via a sidecar map
                _SLOT_OFFSETS[fd] = i
                return fd
            except OSError:
                continue
        time.sleep(0.05 + random.uniform(0, 0.05))


def _release_slot(fd: int | None) -> None:
    if fd is None:
        return
    try:
        offset = _SLOT_OFFSETS.pop(fd, 0)
        fcntl.lockf(fd, fcntl.LOCK_UN, 1, offset)
    finally:
        os.close(fd)


_SLOT_OFFSETS: dict[int, int] = {}


class VendorGate:
    """Vendor semaphores shared by every background job in this process.

    Smartlead also takes a fcntl slot so two workers cannot each run 3-wide.
    """

    def __init__(self) -> None:
        self._sems: dict[str, threading.Semaphore] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._sems.clear()

    def _sem(self, tier: str) -> threading.Semaphore:
        with self._lock:
            if tier not in self._sems:
                self._sems[tier] = threading.Semaphore(vendor_concurrency(tier))
            return self._sems[tier]

    @contextmanager
    def acquire(self, tier: str) -> Iterator[None]:
        sem = self._sem(tier)
        sem.acquire()
        fd: int | None = None
        try:
            if tier in CROSS_PROCESS_TIERS:
                try:
                    fd = _acquire_slot(tier, vendor_concurrency(tier))
                except OSError:
                    fd = None
            yield
        finally:
            _release_slot(fd)
            sem.release()


vendor_gate = VendorGate()


def _retry_delay(attempt: int, response: requests.Response | None) -> float:
    if response is not None:
        retry_after = (response.headers.get("Retry-After") or "").strip()
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        if response.status_code == 429:
            return min(60.0, 5 * (2**attempt)) + random.uniform(0, 0.5)
    return (2**attempt) + random.uniform(0, 0.25)


def request_with_retry(
    tier: str,
    method: str,
    url: str,
    *,
    max_attempts: int | None = None,
    **kwargs: object,
) -> requests.Response | None:
    """Acquire the shared vendor gate; retry 429/5xx with backoff + jitter."""
    attempts = max_attempts if max_attempts is not None else (5 if tier == "smartlead" else 3)
    last: requests.Response | None = None
    with vendor_gate.acquire(tier):
        for attempt in range(attempts):
            try:
                last = requests.request(method, url, **kwargs)  # type: ignore[arg-type]
            except requests.RequestException:
                if attempt < attempts - 1:
                    time.sleep(_retry_delay(attempt, None))
                    continue
                return None
            if last.status_code == 429 or last.status_code >= 500:
                if attempt < attempts - 1:
                    time.sleep(_retry_delay(attempt, last))
                    continue
            return last
    return last
