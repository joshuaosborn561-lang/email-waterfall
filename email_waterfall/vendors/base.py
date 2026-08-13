"""Shared types for enrichment vendor clients."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PersonHit:
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    title: str = ""
    job_level: str = ""
    email: str = ""
    linkedin_url: str = ""
    phone: str = ""
    source_tier: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        if self.full_name:
            return self.full_name
        return f"{self.first_name} {self.last_name}".strip()


@dataclass
class EmailHit:
    email: str
    source_tier: str
    status: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def split_name(full: str) -> tuple[str, str]:
    parts = [p for p in (full or "").strip().split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def person_from_row(row: dict[str, Any], source_tier: str) -> PersonHit | None:
    first = str(row.get("first_name") or "").strip()
    last = str(row.get("last_name") or "").strip()
    full = str(row.get("full_name") or row.get("name") or "").strip()
    if not first and full:
        first, last = split_name(full)
    if not (first or full):
        return None
    return PersonHit(
        first_name=first,
        last_name=last,
        full_name=full or f"{first} {last}".strip(),
        title=str(row.get("title") or row.get("job_title") or row.get("headline") or ""),
        job_level=str(row.get("job_level") or row.get("seniority") or ""),
        email=str(row.get("email") or "").strip().lower(),
        linkedin_url=str(
            row.get("linkedin_url") or row.get("linkedin") or row.get("profile_url") or ""
        ),
        phone=str(row.get("phone") or row.get("mobile") or ""),
        source_tier=source_tier,
        raw=row,
    )
