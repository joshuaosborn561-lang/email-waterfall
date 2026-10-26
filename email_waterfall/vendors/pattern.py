"""Pattern tier: one guess in the company's known email format, checked with
MillionVerifier. Runs after AI Ark and before Prospeo.

Only a MillionVerifier `ok` counts. No known pattern (or a catch-all domain)
means no guess and no MillionVerifier call — this tier never guesses blind.
Guesses are never routed to No2Bounce. Emails found here still go through
Email Verifier Progression before Smartlead.

Patterns live in public.domain_email_patterns (migration 008), read through
the Supabase REST helper in supabase_sync.py.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any
from urllib.parse import quote

from email_waterfall import http_client
from email_waterfall.config import load_settings
from email_waterfall.names import norm_first, norm_last
from email_waterfall.need import CAP_EMAIL, assert_capability

from .base import EmailHit
from .errors import record_response_failure

log = logging.getLogger("email_waterfall.vendors")

MV_URL = "https://api.millionverifier.com/api/v3/"

SUPPORTED_PATTERNS: tuple[str, ...] = (
    "flast",
    "first.last",
    "firstlast",
    "firstl",
    "first",
    "first_last",
    "f.last",
    "lastf",
    "last.first",
    "first-last",
)


def build(first: str, last: str, pattern: str, email_domain: str) -> str | None:
    """Address in `pattern` at `email_domain`, or None (unknown pattern or a
    name part under 2 letters)."""
    f = norm_first(first)
    l = norm_last(last)
    dom = (email_domain or "").strip().lower().lstrip("@")
    if len(f) < 2 or len(l) < 2 or not dom:
        return None
    local = {
        "flast": f[0] + l,
        "first.last": f"{f}.{l}",
        "firstlast": f + l,
        "firstl": f + l[0],
        "first": f,
        "first_last": f"{f}_{l}",
        "f.last": f"{f[0]}.{l}",
        "lastf": l + f[0],
        "last.first": f"{l}.{f}",
        "first-last": f"{f}-{l}",
    }.get((pattern or "").strip().lower())
    if not local:
        return None
    return f"{local}@{dom}"


def _sync():
    from email_waterfall import supabase_sync

    return supabase_sync


class PatternGuessClient:
    tier = "pattern"

    def __init__(self, api_key: str | None = None, timeout: int = 20):
        self.api_key = (
            api_key
            if api_key is not None
            else (os.environ.get("MILLIONVERIFIER_API_KEY") or "").strip()
        )
        self.timeout = timeout
        self.calls = 0
        self.hits = 0
        self.errors = 0
        self.first_error: str | None = None
        self.credits_charged = 0.0
        self.catch_all_marked = 0
        self._patterns: dict[str, tuple[str, str] | None] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and load_settings().supabase_configured

    # -- Supabase ---------------------------------------------------------
    def lookup_pattern(self, domain: str) -> tuple[str, str] | None:
        """Top known (pattern, email_domain) for `domain`; None when unknown or
        the domain is catch-all. Cached per job."""
        dom = (domain or "").strip().lower()
        if dom.startswith("www."):
            dom = dom[4:]
        if not dom:
            return None
        with self._lock:
            if dom in self._patterns:
                return self._patterns[dom]
        found: tuple[str, str] | None = None
        try:
            _status, text = _sync()._request(
                "GET",
                "domain_email_patterns?domain=eq."
                + quote(dom, safe="")
                + "&select=pattern,email_domain,n_verified,is_catch_all"
                + "&order=n_verified.desc",
                prefer="return=representation",
            )
            rows = json.loads(text) if text else []
        except Exception as exc:  # unconfigured / missing table: no guess
            log.warning("pattern lookup failed domain=%s err=%s", dom, str(exc)[:200])
            rows = []
        if isinstance(rows, list) and not any(
            isinstance(r, dict) and r.get("is_catch_all") for r in rows
        ):
            for r in rows:
                if not isinstance(r, dict):
                    continue
                if int(r.get("n_verified") or 0) < 1:
                    continue
                pat = str(r.get("pattern") or "").strip().lower()
                if pat not in SUPPORTED_PATTERNS:
                    continue
                found = (pat, str(r.get("email_domain") or dom).strip().lower())
                break
        with self._lock:
            self._patterns[dom] = found
        return found

    def _mark_catch_all(self, domain: str, email_domain: str, pattern: str) -> None:
        with self._lock:
            self._patterns[domain] = None
            self.catch_all_marked += 1
        try:
            _sync().rpc(
                "ew_mark_domain_catch_all",
                {"p_domain": domain, "p_email_domain": email_domain, "p_pattern": pattern},
            )
        except Exception as exc:
            log.warning("pattern catch_all mark failed domain=%s err=%s", domain, str(exc)[:200])

    def _bump_verified(self, domain: str, email_domain: str, pattern: str) -> None:
        try:
            _sync().rpc(
                "ew_bump_domain_pattern",
                {"p_domain": domain, "p_email_domain": email_domain, "p_pattern": pattern},
            )
        except Exception as exc:
            log.warning("pattern n_verified bump failed domain=%s err=%s", domain, str(exc)[:200])

    # -- MillionVerifier ----------------------------------------------------
    def find_email(self, first: str, last: str, domain: str) -> EmailHit | None:
        if not self.enabled:
            return None
        dom = (domain or "").strip().lower()
        if dom.startswith("www."):
            dom = dom[4:]
        known = self.lookup_pattern(dom)
        if not known:
            return None
        pattern, email_domain = known
        addr = build(first, last, pattern, email_domain)
        if not addr:
            return None
        assert_capability(CAP_EMAIL, vendor=self.tier, endpoint="GET millionverifier /api/v3")
        url = (
            f"{MV_URL}?api={quote(self.api_key, safe='')}"
            f"&email={quote(addr, safe='@')}&timeout=10"
        )
        # One guess per person: exactly one MillionVerifier check.
        self.calls += 1
        self.credits_charged = round(self.credits_charged + 1.0, 6)
        r = http_client.get(self.tier, url, timeout=self.timeout)
        if r is None:
            record_response_failure(self, MV_URL, None)
            return None
        if r.status_code >= 400:
            record_response_failure(self, MV_URL, r)
            return None
        try:
            body: Any = r.json()
        except ValueError:
            record_response_failure(self, MV_URL, r, error="invalid json")
            return None
        if not isinstance(body, dict):
            return None
        if body.get("error"):
            record_response_failure(self, MV_URL, r, error=str(body.get("error"))[:200])
            return None
        result = str(body.get("result") or "").strip().lower()
        if result == "catch_all":
            self._mark_catch_all(dom, email_domain, pattern)
            return None
        if result != "ok":
            return None
        self.hits += 1
        self._bump_verified(dom, email_domain, pattern)
        return EmailHit(
            email=addr,
            source_tier="pattern",
            status="ok",
            raw={"pattern": pattern, "mv": body, "domain": dom},
        )
