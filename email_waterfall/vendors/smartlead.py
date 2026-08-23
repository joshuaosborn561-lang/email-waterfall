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
        self._lock = threading.Lock()
        self._credits_available: int | None = None
        self._credits_total: int | None = None
        self._credits_used: int | None = None
        self._checked_at = 0.0
        self._exhausted = False

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and not self._exhausted

    def _url(self, path: str) -> str:
        qs = urlencode({"api_key": self.api_key})
        return f"{self.base_url}{path}?{qs}"

    def credit_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "available": self._credits_available,
                "total": self._credits_total,
                "used": self._credits_used,
                "exhausted": self._exhausted,
            }

    def refresh_credits(self, *, force: bool = False, ttl: float = 30.0) -> int | None:
        """Read remaining finder credits. None means unknown (fail open)."""
        if not self.api_key:
            return None
        now = time.monotonic()
        with self._lock:
            if (
                not force
                and self._credits_available is not None
                and (now - self._checked_at) < ttl
            ):
                return self._credits_available
        r = http_client.get(
            self.tier,
            self._url(ANALYTICS_PATH),
            headers={"Accept": "application/json"},
            timeout=self.timeout,
        )
        if r is None or r.status_code >= 400:
            return self._credits_available
        try:
            payload = r.json()
        except ValueError:
            return self._credits_available
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
        with self._lock:
            if available is not None:
                self._credits_available = available
                self._exhausted = available <= 0
            if total is not None:
                self._credits_total = total
            if used is not None:
                self._credits_used = used
            self._checked_at = time.monotonic()
            return self._credits_available

    def has_credits(self) -> bool:
        if not self.api_key:
            return False
        if self._exhausted:
            return False
        available = self.refresh_credits()
        if available is None:
            return True
        return available > 0

    def _mark_spent(self, n: int = 1) -> None:
        with self._lock:
            if self._credits_available is not None:
                self._credits_available = max(0, self._credits_available - n)
                if self._credits_used is not None:
                    self._credits_used += n
                if self._credits_available <= 0:
                    self._exhausted = True

    def _mark_exhausted(self) -> None:
        with self._lock:
            self._credits_available = 0
            self._exhausted = True
            self._checked_at = time.monotonic()

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
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=self.timeout,
        )
        if r is None:
            return None
        if r.status_code == 402:
            self._mark_exhausted()
            return None
        try:
            body: Any = r.json()
        except ValueError:
            return None
        if r.status_code >= 400:
            if r.status_code == 429:
                return None
            return None
        if not isinstance(body, dict) or body.get("success") is False:
            message = str(body.get("message") or body.get("error") or "").lower()
            if "credit" in message or "payment" in message:
                self._mark_exhausted()
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
