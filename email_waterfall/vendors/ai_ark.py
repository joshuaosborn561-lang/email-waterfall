"""AI Ark — people, work-email, AND cellphone lookup (not people-only).

Email path (sync, Clay-compatible v2):
  LinkedIn URL or AI Ark person id → POST /v2/people/export/single
  name + domain (or phone) → People Search → person id → export/single

Cellphone path (sync, Clay-compatible v2):
  LinkedIn URL, or name + domain → POST /v2/people/mobile-phone-finder

Do not use /v1/people/email-finder (async trackId). Do not use AI Ark for
email-to-profile reverse lookup.
"""

from __future__ import annotations

from typing import Any

from email_waterfall import http_client
from email_waterfall.config import settings

from .base import EmailHit, PersonHit, PhoneHit, split_name


def _pick_email(payload: Any) -> tuple[str, str]:
    """Return (email, status) from v1 or v2 export/single payloads."""
    if not isinstance(payload, dict):
        return "", ""
    data = payload.get("data") if "data" in payload else payload
    if data is None:
        return "", ""
    if not isinstance(data, dict):
        return "", ""

    block = data.get("email")
    if isinstance(block, str) and "@" in block:
        return block.strip().lower(), "found"
    if isinstance(block, dict):
        value = str(block.get("value") or block.get("address") or "").strip()
        status = str(block.get("state") or block.get("status") or "").strip()
        if value and "@" in value and status.upper() not in {"INVALID", "NOT_FOUND"}:
            return value.lower(), status or "found"
        for item in block.get("output") or []:
            if not isinstance(item, dict) or item.get("found") is False:
                continue
            address = str(item.get("address") or item.get("value") or "").strip()
            item_status = str(item.get("status") or "").upper()
            if address and "@" in address and item_status not in {"INVALID", "NOT_FOUND"}:
                return address.lower(), item_status or "found"

    profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
    flat = str(profile.get("email") or data.get("work_email") or "").strip()
    if flat and "@" in flat:
        return flat.lower(), "found"
    return "", ""


def _ok_phone(value: str) -> bool:
    digits = "".join(c for c in value if c.isdigit())
    return len(digits) >= 7


def _flatten_phones(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = value.strip()
        return [raw] if raw and _ok_phone(raw) else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_flatten_phones(item))
        return out
    return []


def _pick_mobile(payload: Any) -> str:
    """Return first mobile from v2 mobile-phone-finder / export payloads."""
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data") if "data" in payload else payload
    if data is None:
        return ""
    if isinstance(data, list):
        phones = _flatten_phones(data)
        return phones[0] if phones else ""
    if not isinstance(data, dict):
        return ""
    nested = data.get("data")
    phones = _flatten_phones(nested)
    if phones:
        return phones[0]
    for key in ("mobile", "phone", "mobile_number", "cellphone"):
        val = data.get(key)
        if isinstance(val, str) and _ok_phone(val):
            return val.strip()
        phones = _flatten_phones(val)
        if phones:
            return phones[0]
    profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
    for key in ("phone", "mobile", "cellphone"):
        val = str(profile.get(key) or "").strip()
        if val and _ok_phone(val):
            return val
    return ""


def _person_id(row: dict[str, Any]) -> str:
    raw = row.get("id") or row.get("people_id") or ""
    return str(raw).strip()


class AiArkClient:
    tier = "aiark"
    base_url = "https://api.ai-ark.com/api/developer-portal"

    def __init__(self, api_key: str | None = None, timeout: int = 45):
        self.api_key = api_key if api_key is not None else settings.ai_ark_api_key
        self.timeout = timeout
        self.calls = 0
        self.hits = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "X-TOKEN": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _post(self, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        if not self.enabled:
            return 0, None
        self.calls += 1
        r = http_client.post(
            self.tier,
            f"{self.base_url}{path}",
            json=body,
            headers=self._headers(),
            timeout=self.timeout,
        )
        if r is None:
            return 0, None
        try:
            data = r.json()
        except ValueError:
            data = None
        return r.status_code, data

    def _person_from_row(self, row: dict[str, Any]) -> PersonHit | None:
        profile = row.get("profile") if isinstance(row.get("profile"), dict) else row
        first = str(profile.get("first_name") or "").strip()
        last = str(profile.get("last_name") or "").strip()
        full = str(profile.get("full_name") or "").strip()
        if "," in last:
            last = last.split(",", 1)[0].strip()
        if not first and full:
            first, last = split_name(full)
        if not (first or full):
            return None
        link = row.get("link") if isinstance(row.get("link"), dict) else {}
        email, _status = _pick_email(row)
        if not email:
            email = str(profile.get("email") or row.get("email") or "").strip().lower()
            if email and "@" not in email:
                email = ""
        phone = str(
            profile.get("phone")
            or profile.get("mobile")
            or profile.get("cellphone")
            or row.get("phone")
            or row.get("mobile")
            or row.get("cellphone")
            or ""
        ).strip()
        if not phone:
            phone = _pick_mobile(row)
        return PersonHit(
            first_name=first,
            last_name=last,
            full_name=full or f"{first} {last}".strip(),
            title=str(
                profile.get("title")
                or profile.get("headline")
                or row.get("title")
                or ""
            ),
            job_level=str(profile.get("seniority") or row.get("seniority") or ""),
            email=email,
            linkedin_url=str(
                link.get("linkedin")
                or row.get("linkedin_url")
                or profile.get("linkedin_url")
                or ""
            ),
            phone=phone,
            source_tier=self.tier,
            raw=row,
        )

    def find_people(
        self,
        domain: str,
        *,
        company_name: str = "",
        titles: list[str] | None = None,
        limit: int = 10,
        full_name: str = "",
        linkedin_url: str = "",
        phone: str = "",
    ) -> list[PersonHit]:
        if not self.enabled:
            return []
        if not (domain or full_name or linkedin_url or phone or company_name):
            return []
        contact: dict[str, Any] = {}
        if full_name:
            contact["fullName"] = {
                "any": {"include": {"mode": "SMART", "content": [full_name]}}
            }
        if linkedin_url:
            contact["linkedin"] = {"any": {"include": [linkedin_url]}}
        if titles:
            contact["experience"] = {
                "latest": {
                    "title": {
                        "any": {
                            "include": {
                                "mode": "SMART",
                                "content": [t for t in titles if t][:8],
                            }
                        }
                    }
                }
            }
        if phone:
            contact["keyword"] = {
                "any": {
                    "include": {
                        "mode": "WORD",
                        "sources": [{"mode": "WORD", "source": "SUMMARY"}],
                        "content": [phone],
                    }
                }
            }
        body: dict[str, Any] = {
            "page": 0,
            "size": max(1, min(int(limit), 25)),
        }
        if domain:
            body["account"] = {"domain": {"any": {"include": [domain]}}}
        elif company_name:
            body["account"] = {
                "name": {
                    "any": {"include": {"mode": "SMART", "content": [company_name]}}
                }
            }
        if contact:
            body["contact"] = contact
        _status, data = self._post("/v1/people", body)
        if _status >= 400 or not isinstance(data, dict):
            return []

        content: Any = data.get("content") or data.get("data") or data.get("results") or []
        if isinstance(content, dict):
            content = content.get("content") or content.get("results") or []
        out: list[PersonHit] = []
        for row in content[:limit]:
            if not isinstance(row, dict):
                continue
            person = self._person_from_row(row)
            if person:
                out.append(person)
        if out:
            self.hits += 1
        return out

    def _export_single(
        self, *, person_id: str = "", linkedin_url: str = ""
    ) -> EmailHit | None:
        body: dict[str, str] = {}
        if person_id:
            body["id"] = person_id
        if linkedin_url:
            body["url"] = linkedin_url
        if not body:
            return None
        status, data = self._post("/v2/people/export/single", body)
        if status in (0, 400, 401, 402, 429) or status >= 500:
            return None
        # v2 wraps misses as HTTP 200 + data:null (sometimes nested status 404).
        payload = data if isinstance(data, dict) else {}
        email, email_status = _pick_email(payload)
        if not email:
            return None
        self.hits += 1
        return EmailHit(
            email=email,
            source_tier=self.tier,
            status=email_status or "found",
            phone=_pick_mobile(payload),
            raw=payload,
        )

    def find_email(
        self,
        first_name: str = "",
        last_name: str = "",
        domain: str = "",
        company_name: str = "",
        *,
        linkedin_url: str = "",
        phone: str = "",
        person_id: str = "",
        full_name: str = "",
    ) -> EmailHit | None:
        """Work email from LinkedIn URL, AI Ark id, name+domain, and/or phone."""
        if not self.enabled:
            return None
        linkedin_url = (linkedin_url or "").strip()
        person_id = (person_id or "").strip()
        phone = (phone or "").strip()
        full = (full_name or f"{first_name} {last_name}".strip()).strip()
        domain = (domain or "").strip()

        if linkedin_url:
            hit = self._export_single(linkedin_url=linkedin_url)
            if hit:
                return hit

        if person_id:
            hit = self._export_single(person_id=person_id)
            if hit:
                return hit

        company = (company_name or "").strip()
        if not (full or phone or linkedin_url):
            return None
        if not (domain or linkedin_url or phone or company):
            return None

        people = self.find_people(
            domain,
            company_name=company,
            limit=5,
            full_name=full,
            linkedin_url=linkedin_url,
            phone=phone if not (full and (domain or company)) else "",
        )
        for person in people:
            if person.email and "@" in person.email:
                self.hits += 1
                return EmailHit(
                    email=person.email,
                    source_tier=self.tier,
                    status="found",
                    phone=person.phone,
                    raw=person.raw,
                )
            pid = _person_id(person.raw)
            li = person.linkedin_url or linkedin_url
            hit = self._export_single(person_id=pid, linkedin_url=li if not pid else "")
            if hit:
                return hit
        return None

    def find_mobile(
        self,
        first_name: str = "",
        last_name: str = "",
        domain: str = "",
        company_name: str = "",
        *,
        linkedin_url: str = "",
        full_name: str = "",
        work_email: str = "",
    ) -> PhoneHit | None:
        """Cellphone via POST /v2/people/mobile-phone-finder (5 credits on hit)."""
        if not self.enabled:
            return None
        linkedin_url = (linkedin_url or "").strip()
        domain = (domain or "").strip()
        full = (full_name or f"{first_name} {last_name}".strip()).strip()
        body: dict[str, str] = {}
        if linkedin_url:
            body["linkedin"] = linkedin_url
        elif domain and full:
            body["domain"] = domain
            body["name"] = full
        else:
            return None
        status, data = self._post("/v2/people/mobile-phone-finder", body)
        if status in (0, 400, 401, 402, 429) or status >= 500:
            return None
        phone = _pick_mobile(data if isinstance(data, dict) else {})
        if not phone:
            return None
        self.hits += 1
        return PhoneHit(
            phone=phone,
            source_tier=self.tier,
            raw=data if isinstance(data, dict) else {},
        )
