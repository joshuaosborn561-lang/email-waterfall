"""GetLeads — email finder + people discovery."""

from __future__ import annotations

from typing import Any

import requests

from email_waterfall.config import settings

from .base import EmailHit, PersonHit, person_from_row


class GetLeadsClient:
    tier = "getleads"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int = 45,
    ):
        self.api_key = api_key if api_key is not None else settings.getleads_api_key
        self.base_url = (
            base_url if base_url is not None else settings.getleads_base_url
        ).rstrip("/")
        self.find_email_path = settings.getleads_find_email_path
        self.people_path = settings.getleads_people_path
        self.timeout = timeout
        self.calls = 0
        self.hits = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        self.calls += 1
        try:
            r = requests.post(
                url, json=body, headers=self._headers(), timeout=self.timeout
            )
            if r.status_code >= 400:
                return None
            data = r.json()
            return data if isinstance(data, dict) else {"data": data}
        except (requests.RequestException, ValueError):
            return None

    def find_email(
        self, first_name: str, last_name: str, domain: str, company_name: str = ""
    ) -> EmailHit | None:
        data = self._post(
            self.find_email_path,
            {
                "first_name": first_name,
                "last_name": last_name,
                "domain": domain,
                "company_name": company_name or domain,
                "company": company_name or domain,
            },
        )
        if not data:
            return None
        email = (
            data.get("email")
            or (data.get("data") or {}).get("email")
            or (data.get("result") or {}).get("email")
            or ""
        )
        email = str(email).strip().lower()
        if not email or "@" not in email:
            return None
        self.hits += 1
        return EmailHit(
            email=email,
            source_tier=self.tier,
            status=str(data.get("status") or "found"),
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
        body: dict[str, Any] = {
            "domain": domain,
            "company_name": company_name or domain,
            "limit": limit,
        }
        if titles:
            body["titles"] = titles
            body["job_title"] = titles[0]
        data = self._post(self.people_path, body)
        if not data:
            return []
        rows = data.get("people") or data.get("data") or data.get("results") or []
        if isinstance(rows, dict):
            rows = rows.get("people") or rows.get("results") or []
        out: list[PersonHit] = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                continue
            person = person_from_row(row, self.tier)
            if person:
                out.append(person)
        if out:
            self.hits += 1
        return out
