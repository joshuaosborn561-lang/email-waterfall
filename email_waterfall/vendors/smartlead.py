"""Smartlead plan email finder — included monthly allotment, then fall through.

Sits after getleads and before paid tiers. Credits come from
GET /search-email-leads/search-analytics (`availableCredits`). Lookups use
POST /search-email-leads/search-contacts/find-emails (name + domain).
"""

from __future__ import annotations

import threading
import time
from typing import Any
from urllib.parse import urlencode

from email_waterfall import http_client
from email_waterfall.config import settings

from .base import EmailHit

FIND_EMAILS_PATH = "/search-contacts/find-emails"
ANALYTICS_PATH = "/search-analytics"
DEFAULT_BASE = "https://prospect-api.smartlead.ai/api/v1/search-email-leads"

_INVALID_VERIFY = {
    "invalid",
    "not_found",
    "not found",
    "catchall",
    "catch-all",
    "catch_all",
    "unknown",
}
_THROTTLE_MARKERS = (
    "rate limit",
    "rate-limit",
    "ratelimit",
    "too many",
    "throttl",
    "retry later",
    "retry-after",
    "slow down",
)
_CREDIT_ZERO_MARKERS = (
    "insufficient credit",
    "no credit",
    "out of credit",
    "credits exhausted",
    "credit exhausted",
    "allotment exceeded",
    "payment required",
    "no remaining credit",
)


class _SharedCredits:
    """One allotment snapshot for every SmartleadClient in this process."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.available: int | None = None
        self.total: int | None = None
        self.used: int | None = None
        self.exhausted = False
        self.checked_at = 0.0


_CREDITS = _SharedCredits()


def reset_shared_credits() -> None:
    with _CREDITS.lock:
        _CREDITS.available = None
        _CREDITS.total = None
        _CREDITS.used = None
        _CREDITS.exhausted = False
        _CREDITS.checked_at = 0.0


class SmartleadClient:
    tier = "smartlead"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int = 45,
    ):
        self.api_key = api_key if api_key is not None else settings.smartlead_api_key
        self.base_url = (
            base_url if base_url is not None else settings.smartlead_base_url
        ).rstrip("/")
        self.timeout = timeout
        self.calls = 0
        self.hits = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and not _CREDITS.exhausted

    def _url(self, path: str) -> str:
        qs = urlencode({"api_key": self.api_key})
        return f"{self.base_url}{path}?{qs}"

    def credit_snapshot(self) -> dict[str, Any]:
        with _CREDITS.lock:
            return {
                "available": _CREDITS.available,
                "total": _CREDITS.total,
                "used": _CREDITS.used,
                "exhausted": _CREDITS.exhausted,
            }

    def refresh_credits(self, *, force: bool = False, ttl: float = 30.0) -> int | None:
        """Read remaining finder credits. None means unknown (fail open)."""
        if not self.api_key:
            return None
        now = time.monotonic()
        with _CREDITS.lock:
            if (
                not force
                and _CREDITS.available is not None
                and (now - _CREDITS.checked_at) < ttl
            ):
                return _CREDITS.available
        r = http_client.get(
            self.tier,
            self._url(ANALYTICS_PATH),
            headers={"Accept": "application/json"},
            timeout=self.timeout,
        )
        if r is None or r.status_code == 429 or r.status_code >= 500:
            with _CREDITS.lock:
                return _CREDITS.available
        if r.status_code >= 400:
            with _CREDITS.lock:
                return _CREDITS.available
        try:
            payload = r.json()
        except ValueError:
            with _CREDITS.lock:
                return _CREDITS.available
        data = payload.get("data") if isinstance(payload, dict) else None
        block = {}
        if isinstance(data, dict):
            block = data.get("availableCredits") or data.get("available_credits") or {}
        if not isinstance(block, dict):
            block = {}
        available = _as_int(
            block.get("available")
            if "available" in block
            else data.get("available")
            if isinstance(data, dict)
            else None
        )
        total = _as_int(block.get("total"))
        used = _as_int(block.get("used"))
        with _CREDITS.lock:
            if available is not None:
                _CREDITS.available = available
                _CREDITS.exhausted = available <= 0
            if total is not None:
                _CREDITS.total = total
            if used is not None:
                _CREDITS.used = used
            _CREDITS.checked_at = time.monotonic()
            return _CREDITS.available

    def has_credits(self) -> bool:
        if not self.api_key:
            return False
        if _CREDITS.exhausted:
            return False
        available = self.refresh_credits()
        if available is None:
            return True
        return available > 0

    def _mark_spent(self, n: int = 1) -> None:
        with _CREDITS.lock:
            if _CREDITS.available is not None:
                _CREDITS.available = max(0, _CREDITS.available - n)
                if _CREDITS.used is not None:
                    _CREDITS.used += n
                if _CREDITS.available <= 0:
                    _CREDITS.exhausted = True

    def _mark_exhausted(self, *, force: bool = False) -> None:
        with _CREDITS.lock:
            if not force and not _allotment_spent(
                _CREDITS.available, _CREDITS.used, _CREDITS.total
            ):
                return
            _CREDITS.available = 0
            _CREDITS.exhausted = True
            _CREDITS.checked_at = time.monotonic()

    # Test / debug aliases — credit state is process-wide.
    @property
    def _credits_available(self) -> int | None:
        return _CREDITS.available

    @_credits_available.setter
    def _credits_available(self, value: int | None) -> None:
        _CREDITS.available = value

    @property
    def _credits_total(self) -> int | None:
        return _CREDITS.total

    @_credits_total.setter
    def _credits_total(self, value: int | None) -> None:
        _CREDITS.total = value

    @property
    def _credits_used(self) -> int | None:
        return _CREDITS.used

    @_credits_used.setter
    def _credits_used(self, value: int | None) -> None:
        _CREDITS.used = value

    @property
    def _checked_at(self) -> float:
        return _CREDITS.checked_at

    @_checked_at.setter
    def _checked_at(self, value: float) -> None:
        _CREDITS.checked_at = value

    @property
    def _exhausted(self) -> bool:
        return _CREDITS.exhausted

    @_exhausted.setter
    def _exhausted(self, value: bool) -> None:
        _CREDITS.exhausted = value

    def find_email(
        self, first_name: str, last_name: str, domain: str, company_name: str = ""
    ) -> EmailHit | None:
        if not self.enabled:
            return None
        first = (first_name or "").strip()
        last = (last_name or "").strip()
        host = (domain or "").strip().lower()
        if host.startswith("www."):
            host = host[4:]
        if not first or not last or not host:
            return None
        if not self.has_credits():
            return None

        self.calls += 1
        body: Any = None
        r = None
        for attempt in range(4):
            r = http_client.post(
                self.tier,
                self._url(FIND_EMAILS_PATH),
                json={
                    "contacts": [
                        {
                            "firstName": first,
                            "lastName": last,
                            "companyDomain": host,
                        }
                    ]
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
            if r is None:
                return None
            body = _safe_json(r)
            if _is_throttle(r, body):
                if attempt < 3:
                    time.sleep(_throttle_wait(attempt, r))
                    continue
                return None
            break
        if r is None:
            return None
        if getattr(r, "status_code", 0) == 402:
            self._mark_exhausted(force=True)
            return None
        if _is_credit_zero(r, body):
            self._mark_exhausted()
            return None
        if r.status_code >= 400:
            return None
        if not isinstance(body, dict) or body.get("success") is False:
            return None

        self._mark_spent(1)
        row = _first_contact(body)
        email = _pick_email(row)
        status = str(
            (row or {}).get("verification_status")
            or (row or {}).get("status")
            or ""
        ).strip()
        if not email:
            return None
        verify = str((row or {}).get("verification_status") or "").strip().lower()
        if verify.replace(" ", "_") in _INVALID_VERIFY:
            return None
        found = str((row or {}).get("status") or "").strip().lower()
        if found in {"not found", "not_found"}:
            return None
        self.hits += 1
        return EmailHit(
            email=email,
            source_tier=self.tier,
            status=status or "found",
            raw=row or body,
        )


def _safe_json(r: Any) -> Any:
    try:
        return r.json()
    except ValueError:
        return None


def _message_of(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    return str(body.get("message") or body.get("error") or body.get("msg") or "").lower()


def _allotment_spent(
    available: int | None, used: int | None, total: int | None
) -> bool:
    if available is not None and available <= 0:
        return True
    if used is not None and total is not None and total > 0 and used >= total:
        return True
    if available is None and used is None and total is None:
        return True
    return False


def _is_throttle(r: Any, body: Any) -> bool:
    if getattr(r, "status_code", 0) == 429:
        return True
    headers = getattr(r, "headers", None) or {}
    if headers.get("Retry-After") and getattr(r, "status_code", 0) >= 400:
        return True
    msg = _message_of(body)
    if any(token in msg for token in _THROTTLE_MARKERS):
        return True
    if "credit" in msg and not any(token in msg for token in _CREDIT_ZERO_MARKERS):
        snap_used = _CREDITS.used
        snap_total = _CREDITS.total
        if snap_used is not None and snap_total is not None and snap_used < snap_total:
            return True
    return False


def _is_credit_zero(r: Any, body: Any) -> bool:
    if _is_throttle(r, body):
        return False
    if getattr(r, "status_code", 0) == 402:
        return True
    msg = _message_of(body)
    if any(token in msg for token in _CREDIT_ZERO_MARKERS):
        return True
    if "credit" in msg or "payment" in msg:
        return _allotment_spent(_CREDITS.available, _CREDITS.used, _CREDITS.total)
    return False


def _throttle_wait(attempt: int, r: Any) -> float:
    headers = getattr(r, "headers", None) or {}
    retry_after = str(headers.get("Retry-After") or "").strip()
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    return min(60.0, 5 * (2**attempt))


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_contact(body: dict[str, Any]) -> dict[str, Any] | None:
    data = body.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    if isinstance(data, dict):
        rows = data.get("contacts") or data.get("results") or data.get("data") or []
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0]
        return data
    return None


def _pick_email(row: dict[str, Any] | None) -> str:
    if not row:
        return ""
    raw = row.get("email_id") or row.get("email") or row.get("work_email") or ""
    email = str(raw).strip().lower()
    if email and "@" in email and "*" not in email:
        return email
    return ""
