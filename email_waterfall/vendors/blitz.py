"""Blitz — first email tier for IT rows (prepaid Start plan: records, $0).

POST https://api.blitz-api.ai/v2/enrichment/email          {person_linkedin_url}
POST https://api.blitz-api.ai/v2/enrichment/domain-to-linkedin {domain}
POST https://api.blitz-api.ai/v2/search/employee-finder    {company_linkedin_url, job_function, max_results}
GET  https://api.blitz-api.ai/v2/account/key-info          (0 records; records_remaining)
Auth: x-api-key. Docs: https://docs.blitz-api.ai
Rate limit 10 req/s per endpoint on Start -> BLITZ_CONCURRENCY (default 8).
402 = plan out of records: the engine disables the tier for the job.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

import requests

from email_waterfall.concurrency import RateLimiter, vendor_gate
from email_waterfall.names import names_match
from email_waterfall.need import CAP_EMAIL, assert_capability

from .base import EmailHit
from .errors import record_response_failure

BASE_URL = "https://api.blitz-api.ai"
IT_JOB_FUNCTION = "Information Technology"  # exact Blitz job_function enum value
RETRY_DELAYS = (1.5, 4.0)
DEFAULT_MIN_RECORDS_LEFT = 2000
DEFAULT_TIMEOUT = 15

_blitz_rate = RateLimiter(10.0)


def min_records_left() -> int:
    raw = (os.environ.get("BLITZ_MIN_RECORDS_LEFT") or "").strip()
    try:
        return int(raw) if raw else DEFAULT_MIN_RECORDS_LEFT
    except ValueError:
        return DEFAULT_MIN_RECORDS_LEFT


def _email_from(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    for src in (body, body.get("data") if isinstance(body.get("data"), dict) else None):
        if not isinstance(src, dict):
            continue
        if src.get("found") is False:
            return ""
        for key in ("email", "work_email", "email_address"):
            val = src.get(key)
            if isinstance(val, str) and "@" in val:
                return val.strip().lower()
        emails = src.get("emails") or src.get("all_emails")
        if isinstance(emails, list) and emails:
            first = emails[0]
            if isinstance(first, str) and "@" in first:
                return first.strip().lower()
            if isinstance(first, dict):
                val = first.get("email")
                if isinstance(val, str) and "@" in val:
                    return val.strip().lower()
    return ""


class BlitzClient:
    tier = "blitz"

    def __init__(
        self,
        api_key: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.api_key = (
            api_key if api_key is not None else (os.environ.get("BLITZ_API_KEY") or "").strip()
        )
        self.timeout = timeout
        self.sleep = sleep
        self.calls = 0
        self.hits = 0
        self.errors = 0
        self.first_error: str | None = None
        self.credits_charged = 0.0
        self.out_of_records = False
        self.records_remaining: int | None = None
        self._company_cache: dict[str, str] = {}
        self._employee_cache: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and not self.out_of_records

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "Content-Type": "application/json"}

    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        """One Blitz call with 1.5s / 4s backoff on 429 and 5xx. None on failure."""
        url = f"{BASE_URL}{path}"
        resp: requests.Response | None = None
        with vendor_gate.acquire(self.tier):
            for attempt in range(len(RETRY_DELAYS) + 1):
                _blitz_rate.wait()
                try:
                    resp = requests.request(
                        method, url, json=body, headers=self._headers(), timeout=self.timeout
                    )
                except requests.RequestException as exc:
                    resp = None
                    if attempt < len(RETRY_DELAYS):
                        self.sleep(RETRY_DELAYS[attempt])
                        continue
                    record_response_failure(self, url, None, error=type(exc).__name__)
                    return None
                if resp.status_code == 429 or resp.status_code >= 500:
                    if attempt < len(RETRY_DELAYS):
                        self.sleep(RETRY_DELAYS[attempt])
                        continue
                break
        if resp is None:
            return None
        if resp.status_code == 402:
            self.out_of_records = True
            record_response_failure(self, url, resp, error="out of records")
            return None
        if resp.status_code == 404:
            return {"found": False}
        if resp.status_code >= 400:
            record_response_failure(self, url, resp)
            return None
        try:
            data = resp.json()
        except ValueError:
            record_response_failure(self, url, resp, error="invalid json")
            return None
        fair = data.get("fair_usage") if isinstance(data, dict) else None
        if isinstance(fair, dict) and isinstance(fair.get("records_remaining"), (int, float)):
            self.records_remaining = int(fair["records_remaining"])
        return data

    def key_info(self) -> dict[str, Any] | None:
        data = self._request("GET", "/v2/account/key-info")
        if isinstance(data, dict) and isinstance(data.get("records_remaining"), (int, float)):
            self.records_remaining = int(data["records_remaining"])
        return data if isinstance(data, dict) else None

    def find_email(self, linkedin_url: str) -> EmailHit | None:
        url = (linkedin_url or "").strip()
        if not self.enabled or not url:
            return None
        assert_capability(CAP_EMAIL, vendor=self.tier, endpoint="POST /v2/enrichment/email")
        self.calls += 1
        data = self._request("POST", "/v2/enrichment/email", {"person_linkedin_url": url})
        email = _email_from(data)
        if not email:
            return None
        self.hits += 1
        self.credits_charged = round(self.credits_charged + 1.0, 6)
        return EmailHit(email=email, source_tier="blitz", status="found", raw=data or {})

    def company_linkedin(self, domain: str) -> str:
        """Domain -> company LinkedIn URL, cached per job (misses too)."""
        dom = (domain or "").strip().lower()
        if not dom or not self.enabled:
            return ""
        with self._lock:
            if dom in self._company_cache:
                return self._company_cache[dom]
        self.calls += 1
        data = self._request("POST", "/v2/enrichment/domain-to-linkedin", {"domain": dom})
        url = ""
        if isinstance(data, dict) and data.get("found") is not False:
            url = str(data.get("company_linkedin_url") or "").strip()
            if url:
                self.credits_charged = round(self.credits_charged + 1.0, 6)
        if data is not None or self.out_of_records:
            with self._lock:
                self._company_cache[dom] = url
        return url

    def it_employees(self, company_linkedin_url: str) -> list[dict[str, Any]]:
        """IT employees at one company (max 50), cached per job."""
        key = (company_linkedin_url or "").strip().lower()
        if not key or not self.enabled:
            return []
        with self._lock:
            if key in self._employee_cache:
                return self._employee_cache[key]
        self.calls += 1
        data = self._request(
            "POST",
            "/v2/search/employee-finder",
            {
                "company_linkedin_url": company_linkedin_url,
                "job_function": [IT_JOB_FUNCTION],
                "max_results": 50,
            },
        )
        rows = []
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            rows = [r for r in data["results"] if isinstance(r, dict)]
            self.credits_charged = round(self.credits_charged + len(rows), 6)
        if data is not None:
            with self._lock:
                self._employee_cache[key] = rows
        return rows

    def find_linkedin(self, first: str, last: str, domain: str) -> str:
        company = self.company_linkedin(domain)
        if not company:
            return ""
        for emp in self.it_employees(company):
            if names_match(first, last, str(emp.get("first_name") or ""), str(emp.get("last_name") or "")):
                url = str(emp.get("linkedin_url") or "").strip()
                if "linkedin.com/in/" in url:
                    return url
        return ""
