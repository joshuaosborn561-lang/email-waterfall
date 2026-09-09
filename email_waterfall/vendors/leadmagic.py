"""LeadMagic — email finder, ranked role finder, and mobile finder."""

from __future__ import annotations

from typing import Any

from email_waterfall import http_client
from email_waterfall.config import settings
from email_waterfall.need import CAP_EMAIL, CAP_PEOPLE, CAP_PHONE, assert_capability

from .base import EmailHit, PersonHit, PhoneHit, person_from_row


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
        r = http_client.post(
            self.tier,
            url,
            json=body,
            headers=self._headers(),
            timeout=self.timeout,
        )
        if r is None:
            return None
        try:
            if r.status_code >= 400:
                return None
            return r.json()
        except ValueError:
            return None

    def find_email(
        self, first_name: str, last_name: str, domain: str = "", company_name: str = ""
    ) -> EmailHit | None:
        first = (first_name or "").strip()
        last = (last_name or "").strip()
        domain = (domain or "").strip()
        company = (company_name or "").strip()
        if not first or not last or not (domain or company):
            return None
        assert_capability(
            CAP_EMAIL, vendor=self.tier, endpoint="POST /v1/people/email-finder"
        )
        body: dict[str, Any] = {
            "first_name": first,
            "last_name": last,
        }
        if domain:
            body["domain"] = domain
        if company:
            body["company_name"] = company
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
        assert_capability(
            CAP_PEOPLE, vendor=self.tier, endpoint="POST /v1/people/role-finder"
        )
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

    def find_mobile(
        self,
        *,
        linkedin_url: str = "",
        work_email: str = "",
        personal_email: str = "",
    ) -> PhoneHit | None:
        """Cellphone via POST /v1/people/mobile-finder (5 credits on hit, free miss)."""
        if not self.enabled:
            return None
        body: dict[str, Any] = {}
        profile = (linkedin_url or "").strip()
        work = (work_email or "").strip().lower()
        personal = (personal_email or "").strip().lower()
        if profile:
            body["profile_url"] = profile
        if work:
            body["work_email"] = work
        if personal:
            body["personal_email"] = personal
        if not body:
            return None
        assert_capability(
            CAP_PHONE, vendor=self.tier, endpoint="POST /v1/people/mobile-finder"
        )
        data = self._post("/v1/people/mobile-finder", body)
        if data is None:
            data = self._post("/mobile-finder", body)
        if not isinstance(data, dict):
            return None
        mobile = (
            data.get("mobile_number")
            or data.get("mobile")
            or data.get("phone")
            or data.get("cellphone")
            or ""
        )
        mobile = str(mobile).strip() if mobile is not None else ""
        digits = "".join(c for c in mobile if c.isdigit())
        if not mobile or len(digits) < 7:
            return None
        self.hits += 1
        return PhoneHit(phone=mobile, source_tier=self.tier, raw=data)
