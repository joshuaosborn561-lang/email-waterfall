"""Which rows count as IT (Blitz runs first for them). First match wins:

1. `it_lane=true` request param (enrich_waterfall / enrich_person / /enrich-one)
2. `it_lane: true` in the client's public.wf_client_profiles profile document
3. title match against IT_TITLE_PATTERNS (case-insensitive, word boundaries),
   unless the title hits IT_TITLE_EXCLUDES
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import quote

log = logging.getLogger("email_waterfall.it_lane")

IT_TITLE_PATTERNS: tuple[str, ...] = (
    "CIO",
    "CTO",
    "CISO",
    "chief information",
    "chief technology",
    "chief information security",
    "IT director",
    "director of IT",
    "director of information technology",
    "VP of IT",
    "VP IT",
    "vice president of information technology",
    "head of IT",
    "IT manager",
    "information technology",
    "infrastructure",
    "network",
    "systems",
    "sysadmin",
    "system administrator",
    "security engineer",
    "cybersecurity",
    "information security",
    "help desk",
    "service desk",
    "technology director",
    "director of technology",
    "IT operations",
)
IT_TITLE_EXCLUDES: tuple[str, ...] = (
    "nurse",
    "nursing",
    "sales",
    "account executive",
    "recruit",
    "HR",
    "human resources",
)

# "IT" must stay case-sensitive-ish: match the literal uppercase word "IT"
# (or "I.T.") so the English word "it" never counts.
def _compile(phrases: tuple[str, ...]) -> list[re.Pattern[str]]:
    out = []
    for p in phrases:
        parts = []
        for word in p.split():
            if word == "IT":
                parts.append(r"(?-i:IT|I\.T\.)")
            elif word == "recruit":
                parts.append(r"recruit\w*")
            else:
                parts.append(re.escape(word))
        out.append(re.compile(r"(?i)(?<![A-Za-z])" + r"[\s,/&-]+".join(parts) + r"(?![A-Za-z])"))
    return out


_INCLUDE = _compile(IT_TITLE_PATTERNS)
_EXCLUDE = _compile(IT_TITLE_EXCLUDES)


def title_is_it(title: str) -> bool:
    t = (title or "").strip()
    if not t:
        return False
    if any(p.search(t) for p in _EXCLUDE):
        return False
    return any(p.search(t) for p in _INCLUDE)


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def it_row_reason(
    row: dict[str, Any],
    client_profile: dict[str, Any] | None,
    it_lane_param: bool | None,
) -> str | None:
    if it_lane_param is not None and _truthy(it_lane_param):
        return "param"
    if isinstance(client_profile, dict) and _truthy(client_profile.get("it_lane")):
        return "profile"
    if title_is_it(str((row or {}).get("title") or "")):
        return "title"
    return None


def is_it_row(
    row: dict[str, Any],
    client_profile: dict[str, Any] | None,
    it_lane_param: bool | None,
) -> bool:
    return it_row_reason(row, client_profile, it_lane_param) is not None


def load_client_profile(client_tag: str) -> dict[str, Any] | None:
    """profile jsonb from public.wf_client_profiles (None when absent/unconfigured)."""
    tag = (client_tag or "").strip()
    if not tag:
        return None
    try:
        from email_waterfall import supabase_sync
        from email_waterfall.config import load_settings

        if not load_settings().supabase_configured:
            return None
        _status, text = supabase_sync._request(
            "GET",
            f"wf_client_profiles?client_tag=eq.{quote(tag, safe='')}&select=profile",
            prefer="return=representation",
        )
        rows = json.loads(text) if text else []
    except Exception as exc:
        log.warning("wf_client_profiles read failed tag=%s err=%s", tag, str(exc)[:200])
        return None
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        prof = rows[0].get("profile")
        if isinstance(prof, str):
            try:
                prof = json.loads(prof)
            except ValueError:
                prof = None
        return prof if isinstance(prof, dict) else None
    return None
