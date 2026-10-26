"""Serper (Google search, $0.001/query) — last LinkedIn URL resolver for IT rows.

POST https://google.serper.dev/search, header X-API-KEY (SERPER_API_KEY).
One query per person, max.
"""

from __future__ import annotations

import os
import re
from typing import Any

from email_waterfall import http_client
from email_waterfall.names import norm_first, norm_last

from .errors import record_response_failure

SERPER_URL = "https://google.serper.dev/search"


def _norm_text(text: str) -> str:
    import unicodedata

    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", t)


def _company_terms(company_name: str, domain: str) -> list[str]:
    terms = []
    name = _norm_text(company_name).strip()
    if name:
        terms.append(name)
    root = (domain or "").lower().split(".")[0] if domain else ""
    if root and len(root) >= 3:
        terms.append(root)
    return terms


class SerperClient:
    tier = "serper"

    def __init__(self, api_key: str | None = None, timeout: int = 10):
        self.api_key = (
            api_key if api_key is not None else (os.environ.get("SERPER_API_KEY") or "").strip()
        )
        self.timeout = timeout
        self.calls = 0
        self.hits = 0
        self.errors = 0
        self.first_error: str | None = None
        self.credits_charged = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def find_linkedin(self, first: str, last: str, company_name: str, domain: str = "") -> str:
        if not self.enabled:
            return ""
        f, l = norm_first(first), norm_last(last)
        terms = _company_terms(company_name, domain)
        if not f or not l or not terms:
            return ""
        label = company_name or domain
        q = f'site:linkedin.com/in "{first.strip()} {last.strip()}" "{label}"'
        self.calls += 1
        self.credits_charged = round(self.credits_charged + 1.0, 6)
        r = http_client.post(
            self.tier,
            SERPER_URL,
            json={"q": q, "num": 5},
            headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
            timeout=self.timeout,
        )
        if r is None or r.status_code >= 400:
            record_response_failure(self, SERPER_URL, r)
            return ""
        try:
            body: Any = r.json()
        except ValueError:
            return ""
        organic = body.get("organic") if isinstance(body, dict) else None
        for item in organic or []:
            if not isinstance(item, dict):
                continue
            link = str(item.get("link") or "")
            if "linkedin.com/in/" not in link:
                continue
            # Top linkedin.com/in/ result only.
            title = _norm_text(str(item.get("title") or ""))
            snippet = _norm_text(str(item.get("snippet") or ""))
            words = set(title.split())
            if f in words and l in words and any(t in title or t in snippet for t in terms):
                self.hits += 1
                return link
            return ""
        return ""
