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
from typing import Callable, Iterator

import requests

from email_waterfall.errors import VendorCallError

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

DEFAULT_COMPANY_CONCURRENCY = 12
DEFAULT_JOB_CONCURRENCY = 12
MAX_JOB_CONCURRENCY = 32
WRITEBACK_BATCH = 200
ROW_ERROR_RETRIES = 3

_req_lock = threading.Lock()
_requests_made = 0
_last_progress_at = 0.0
_active_tier = ""
_throttle_hook: Callable[[], None] | None = None


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def clamp_concurrency(value: int) -> int:
    return max(1, min(MAX_JOB_CONCURRENCY, int(value)))


def job_concurrency(override: int | None = None) -> int:
    """Row worker pool size. TIER_CONCURRENCY default 12, hard cap 32."""
    if override is not None:
        return clamp_concurrency(override)
    if (os.environ.get("TIER_CONCURRENCY") or "").strip():
        return clamp_concurrency(_env_int("TIER_CONCURRENCY", DEFAULT_JOB_CONCURRENCY))
    if (os.environ.get("COMPANY_CONCURRENCY") or "").strip():
        return clamp_concurrency(_env_int("COMPANY_CONCURRENCY", DEFAULT_JOB_CONCURRENCY))
    return DEFAULT_JOB_CONCURRENCY


def company_concurrency() -> int:
    return job_concurrency()


def note_request(tier: str) -> None:
    global _requests_made, _last_progress_at, _active_tier
    with _req_lock:
        _requests_made += 1
        _last_progress_at = time.time()
        _active_tier = tier


def request_stats() -> dict[str, object]:
    with _req_lock:
        return {
            "requests_made": _requests_made,
            "last_progress_at": _last_progress_at,
            "active_tier": _active_tier,
        }


def reset_request_stats() -> None:
    global _requests_made, _last_progress_at, _active_tier
    with _req_lock:
        _requests_made = 0
        _last_progress_at = 0.0
        _active_tier = ""


def set_throttle_hook(hook: Callable[[], None] | None) -> None:
    global _throttle_hook
    _throttle_hook = hook


def notify_throttle() -> None:
    hook = _throttle_hook
    if hook is not None:
        hook()


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


class WorkerSlots:
    """Bounded worker slots that shrink for a cooldown after a 429."""

    def __init__(self, size: int) -> None:
        self.size = clamp_concurrency(size)
        self._sem = threading.Semaphore(self.size)
        self._lock = threading.Lock()
        self.effective = self.size

    def acquire(self) -> None:
        self._sem.acquire()

    def release(self) -> None:
        self._sem.release()

    def shrink(self, seconds: float = 20.0) -> None:
        with self._lock:
            if self.effective <= 1:
                return
            if not self._sem.acquire(blocking=False):
                return
            self.effective -= 1
        timer = threading.Timer(max(0.05, float(seconds)), self._restore)
        timer.daemon = True
        timer.start()

    def _restore(self) -> None:
        self._sem.release()
        with self._lock:
            if self.effective < self.size:
                self.effective += 1


def stall_seconds() -> float:
    raw = (os.environ.get("STALL_SECONDS") or "").strip()
    if raw:
        try:
            return max(0.05, float(raw))
        except ValueError:
            pass
    return 300.0


def request_with_retry(
    tier: str,
    method: str,
    url: str,
    *,
    max_attempts: int | None = None,
    **kwargs: object,
) -> requests.Response | None:
    """Acquire the shared vendor gate, retry 429/5xx with backoff + jitter."""
    attempts = max_attempts if max_attempts is not None else (5 if tier == "smartlead" else 3)
    last: requests.Response | None = None
    last_kind = "http"
    with vendor_gate.acquire(tier):
        for attempt in range(attempts):
            try:
                note_request(tier)
                last = requests.request(method, url, **kwargs)  # type: ignore[arg-type]
            except requests.RequestException as exc:
                last_kind = "timeout"
                if attempt < attempts - 1:
                    time.sleep(_retry_delay(attempt, None))
                    continue
                raise VendorCallError(tier, "timeout", str(exc)) from exc
            if last.status_code == 429:
                last_kind = "throttle"
                notify_throttle()
                if attempt < attempts - 1:
                    time.sleep(_retry_delay(attempt, last))
                    continue
                raise VendorCallError(tier, "throttle", "429")
            if last.status_code >= 500:
                last_kind = "http"
                if attempt < attempts - 1:
                    time.sleep(_retry_delay(attempt, last))
                    continue
                raise VendorCallError(tier, "http", f"status {last.status_code}")
            return last
    if last is None:
        raise VendorCallError(tier, last_kind, "no response")
    return last
