"""AI Ark — people discovery only (never email-to-profile reverse lookup)."""

from __future__ import annotations

from typing import Any

import requests

from email_waterfall.config import settings

from .base import PersonHit, split_name


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

    def find_people(
        self,
        domain: str,
        *,
        company_name: str = "",
        titles: list[str] | None = None,
        limit: int = 10,
    ) -> list[PersonHit]:
        if not self.enabled or not domain:
            return []
        body: dict[str, Any] = {
            "page": 0,
            "size": max(1, min(int(limit), 25)),
            "account": {"domain": {"any": {"include": [domain]}}},
        }
        self.calls += 1
        try:
            r = requests.post(
                f"{self.base_url}/v1/people",
                json=body,
                headers=self._headers(),
                timeout=self.timeout,
            )
            if r.status_code >= 400:
                return []
            data = r.json()
        except (requests.RequestException, ValueError):
            return []

        content: Any = []
        if isinstance(data, dict):
            content = data.get("content") or data.get("data") or data.get("results") or []
            if isinstance(content, dict):
                content = content.get("content") or content.get("results") or []
        out: list[PersonHit] = []
        for row in content[:limit]:
            if not isinstance(row, dict):
                continue
            profile = row.get("profile") if isinstance(row.get("profile"), dict) else row
            first = str(profile.get("first_name") or "").strip()
            last = str(profile.get("last_name") or "").strip()
            full = str(profile.get("full_name") or "").strip()
            if "," in last:
                last = last.split(",", 1)[0].strip()
            if not first and full:
                first, last = split_name(full)
            if not (first or full):
                continue
            link = row.get("link") if isinstance(row.get("link"), dict) else {}
            title = str(
                profile.get("title")
                or profile.get("headline")
                or row.get("title")
                or ""
            )
            out.append(
                PersonHit(
                    first_name=first,
                    last_name=last,
                    full_name=full or f"{first} {last}".strip(),
                    title=title,
                    job_level=str(profile.get("seniority") or row.get("seniority") or ""),
                    linkedin_url=str(
                        link.get("linkedin")
                        or row.get("linkedin_url")
                        or profile.get("linkedin_url")
                        or ""
                    ),
                    source_tier=self.tier,
                    raw=row,
                )
            )
        if out:
            self.hits += 1
        return out
