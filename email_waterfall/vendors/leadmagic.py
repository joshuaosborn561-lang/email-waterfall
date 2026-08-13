"""LeadMagic — email finder + ranked role finder."""

from __future__ import annotations

from typing import Any

import requests

from email_waterfall.config import settings

from .base import EmailHit, PersonHit, person_from_row


class LeadMagicClient:
    tier = "leadmagic"
    base_url = "https://api.leadmagic.io"

    def __init__(self, api_key: str | None = None, timeout: int = 45):
        self.api_key = api_key if api_key is not None else settings.leadmagic_api_key
        self.timeout = timeout
        self.calls = 0
        self.hits = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any] | list | None:
        if not self.enabled:
            return None
        url = f"{self.base_url}{path}"
        self.calls += 1
        try:
            r = requests.post(
                url, json=body, headers=self._headers(), timeout=self.timeout
            )
            if r.status_code >= 400:
                return None
            return r.json()
        except (requests.RequestException, ValueError):
            return None

    def find_email(
        self, first_name: str, last_name: str, domain: str, company_name: str = ""
    ) -> EmailHit | None:
        body: dict[str, Any] = {
            "first_name": first_name,
            "last_name": last_name,
            "domain": domain,
        }
        if company_name:
            body["company_name"] = company_name
        data = self._post("/v1/people/email-finder", body)
        if data is None:
            data = self._post("/email-finder", body)
        if not isinstance(data, dict):
            return None
        email = str(data.get("email") or "").strip().lower()
        status = str(data.get("status") or "")
        if not email or status in {"not_found", "invalid"}:
            return None
        self.hits += 1
        return EmailHit(
            email=email,
            source_tier=self.tier,
            status=status or "valid",
            raw=data,
        )

    def find_people(
        self,
        domain: str,
        *,
        company_name: str = "",
        titles: list[str] | None = None,
        limit: int = 10,
    ) -> list[PersonHit]:
        """Role-finder per ranked title, then employee-finder as a broader net."""
        if not self.enabled or not domain:
            return []
        found: list[PersonHit] = []
        seen: set[tuple[str, str]] = set()

        def _absorb(rows: Any) -> None:
            if isinstance(rows, dict):
                rows = (
                    rows.get("data")
                    or rows.get("people")
                    or rows.get("results")
                    or rows.get("contacts")
                    or []
                )
            if not isinstance(rows, list):
                return
            for row in rows:
                if not isinstance(row, dict):
                    continue
                person = person_from_row(row, self.tier)
                if not person:
                    continue
                key = (person.first_name.lower(), person.last_name.lower())
                if key in seen:
                    continue
                seen.add(key)
                found.append(person)

        # Ranked titles first — stop once we have people for a primary title.
        for title in (titles or [])[:8]:
            body = {
                "job_title": title,
                "company_domain": domain,
                "domain": domain,
                "company_name": company_name or domain,
            }
            data = self._post("/v1/people/role-finder", body)
            if data is None:
                data = self._post("/role-finder", body)
            before = len(found)
            _absorb(data)
            if len(found) > before:
                self.hits += 1
                if len(found) >= limit:
                    return found[:limit]

        if len(found) < limit:
            data = self._post(
                "/employee-finder",
                {
                    "company_name": company_name or domain,
                    "company_domain": domain,
                    "domain": domain,
                    "per_page": min(limit, 20),
                    "page": 1,
                },
            )
            before = len(found)
            _absorb(data)
            if len(found) > before:
                self.hits += 1

        return found[:limit]
