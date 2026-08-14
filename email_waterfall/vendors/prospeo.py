"""Prospeo — email tier between LeadMagic and FullEnrich.

POST https://api.prospeo.io/enrich-person
Auth: X-KEY
Docs: https://prospeo.io/api-docs/enrich-person
"""

from __future__ import annotations

from typing import Any

import requests

from email_waterfall.config import settings

from .base import EmailHit


class ProspeoClient:
    tier = "prospeo"
    base_url = "https://api.prospeo.io"

    def __init__(self, api_key: str | None = None, timeout: int = 45):
        self.api_key = api_key if api_key is not None else settings.prospeo_api_key
        self.timeout = timeout
        self.calls = 0
        self.hits = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "X-KEY": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def find_email(
        self,
        first_name: str = "",
        last_name: str = "",
        domain: str = "",
        company_name: str = "",
        *,
        linkedin_url: str = "",
        full_name: str = "",
    ) -> EmailHit | None:
        if not self.enabled:
            return None
        data: dict[str, str] = {}
        if linkedin_url:
            data["linkedin_url"] = linkedin_url.strip()
        if first_name:
            data["first_name"] = first_name.strip()
        if last_name:
            data["last_name"] = last_name.strip()
        full = (full_name or f"{first_name} {last_name}".strip()).strip()
        if full and not (first_name and last_name):
            data["full_name"] = full
        if domain:
            data["company_website"] = domain.strip()
        if company_name:
            data["company_name"] = company_name.strip()

        has_linkedin = bool(data.get("linkedin_url"))
        has_name_company = bool(
            ((data.get("first_name") and data.get("last_name")) or data.get("full_name"))
            and (
                data.get("company_website")
                or data.get("company_name")
                or data.get("company_linkedin_url")
            )
        )
        if not has_linkedin and not has_name_company:
            return None

        self.calls += 1
        try:
            r = requests.post(
                f"{self.base_url}/enrich-person",
                json={
                    "only_verified_email": True,
                    "enrich_mobile": False,
                    "data": data,
                },
                headers=self._headers(),
                timeout=self.timeout,
            )
            body: Any
            try:
                body = r.json()
            except ValueError:
                return None
        except requests.RequestException:
            return None

        if not isinstance(body, dict):
            return None
        if r.status_code >= 400 or body.get("error") is True:
            return None

        person = body.get("person") if isinstance(body.get("person"), dict) else {}
        email_obj = person.get("email") if isinstance(person.get("email"), dict) else {}
        email = str(email_obj.get("email") or "").strip().lower()
        status = str(email_obj.get("status") or "").strip()
        if not email or "@" not in email or "*" in email:
            return None
        if status.upper() in {"INVALID", "NOT_FOUND"}:
            return None
        self.hits += 1
        return EmailHit(
            email=email,
            source_tier=self.tier,
            status=status or "VERIFIED",
            raw=body,
        )
