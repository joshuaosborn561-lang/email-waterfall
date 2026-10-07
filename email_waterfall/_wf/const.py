"""DM / work-email enrichment waterfall.

Tiers (fixed, no Maps, no website crawl, no Apify):
  getleads → Smartlead (plan email finder) → AI Ark → LeadMagic → Prospeo → FullEnrich

AI Ark is third on BOTH lanes after the included Smartlead allotment is used:
  people/DM: People Search by domain
  email: LinkedIn URL / person id / name+domain / phone → export/single

Also fills cellphone: AI Ark mobile-phone-finder (LinkedIn or name+domain),
then LeadMagic mobile-finder, then Prospeo, then FullEnrich if max_tier
allows. FullEnrich requests contact.phones (most_probable_phone / MOBILE).

On need='phone' only, every candidate number (input or vendor) is checked
with Veriphone GET /v2/verify. Only phone_type=mobile is written as cellphone.
Landline / voip / invalid fall through to the next finder. The number and
Veriphone phone_type are written back to the source table as wf_phone /
wf_phone_type, and to {client}_*contacts.line_type. need='both' / 'email'
skip Veriphone unless verify_only=True (check numbers we already have; no
finder spend).

`need` is an allowlist of vendor capabilities (email / phone / people), applied
before any vendor HTTP call. need='email' must not call phone endpoints.
AI Ark search-then-export is one attempt and two vendor_calls.

Writes to public.{client}_companies / public.{client}_contacts.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

from email_waterfall.clients import ClientConfig
from email_waterfall.need import (
    capabilities,
    credit_per_row,
    estimate_suppressed,
    resolve_need,
    tier_serves_need,
)
from email_waterfall.vendors.base import split_name
from email_waterfall.vendors.smartlead import SmartleadClient

log = logging.getLogger("email_waterfall.waterfall")

Need = Literal["email", "dm", "both", "phone", "people_email"]
MaxTier = Literal["getleads", "smartlead", "aiark", "leadmagic", "prospeo", "fullenrich"]

TIER_ORDER: list[str] = [
    "getleads",
    "smartlead",
    "aiark",
    "leadmagic",
    "prospeo",
    "fullenrich",
]
TIER_RANK = {name: i for i, name in enumerate(TIER_ORDER)}
DEFAULT_MAX_TIER: MaxTier = "leadmagic"
NAME_COMPANY_TIERS: tuple[str, ...] = (
    "getleads",
    "aiark",
    "leadmagic",
    "prospeo",
    "fullenrich",
)
CIRCUIT_WINDOW = 25
CIRCUIT_ERROR_LIMIT = 20
CREDIT_PER_ATTEMPT: dict[str, float] = {
    "getleads": 1.0,
    "smartlead": 1.0,
    "aiark": 1.5,
    "leadmagic": 1.0,
    "prospeo": 1.0,
    "fullenrich": 1.0,
}


def normalize_max_tier(max_tier: str | None) -> str:
    t = (max_tier or DEFAULT_MAX_TIER).strip().lower()
    aliases = {
        "ai_ark": "aiark",
        "ai-ark": "aiark",
        "full_enrich": "fullenrich",
        "full-enrich": "fullenrich",
        "fe": "fullenrich",
        "get_leads": "getleads",
        "smart_lead": "smartlead",
        "smart-lead": "smartlead",
        "sl": "smartlead",
        "lead_magic": "leadmagic",
        "lm": "leadmagic",
        "prospector": "prospeo",
        "apify": "getleads",  # Apify is not in this service; start at first paid tier
    }
    t = aliases.get(t, t)
    if t not in TIER_RANK:
        raise ValueError(
            f"max_tier must be one of {', '.join(TIER_ORDER)}; got {max_tier!r}"
        )
    return t


def tier_allowed(tier: str, max_tier: str) -> bool:
    return TIER_RANK[tier] <= TIER_RANK[normalize_max_tier(max_tier)]


def _parse_rows(rows: Any) -> list[dict[str, Any]]:
    if rows is None:
        return []
    if isinstance(rows, str):
        rows = json.loads(rows) if rows.strip() else []
    if isinstance(rows, dict) and "rows" in rows:
        rows = rows["rows"]
    if not isinstance(rows, list):
        raise ValueError("rows must be a JSON list of objects")
    return [r for r in rows if isinstance(r, dict)]


def _has_inline_rows(rows: Any) -> bool:
    """True when the caller passed a non-empty inline payload.

    MCP hosts often fill unused optional params as null / [] / {}.
    Those must not collide with source_table.
    """
    if rows is None or rows == "" or rows == [] or rows == {}:
        return False
    if isinstance(rows, str) and not rows.strip():
        return False
    if isinstance(rows, str) and rows.strip() in ("[]", "{}", "null"):
        return False
    return True


def _host_from(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    host = urlsplit(raw).hostname or ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _norm_row(r: dict[str, Any]) -> dict[str, Any]:
    domain = _host_from(str(r.get("domain") or r.get("website") or ""))
    first = str(r.get("first_name") or "").strip()
    last = str(r.get("last_name") or "").strip()
    full = str(
        r.get("name") or r.get("full_name") or r.get("owner_name") or ""
    ).strip()
    if not first and full:
        first, last = split_name(full)
    email = str(r.get("email") or "").strip().lower()
    if not domain and email and "@" in email:
        domain = _host_from(email.split("@", 1)[1])
    company = str(
        r.get("company_name") or r.get("company") or r.get("business_name") or ""
    ).strip()
    mode = "domain" if domain else (
        "name_company" if first and last and company else ""
    )
    return {
        "domain": domain,
        "company_name": company,
        "first_name": first,
        "last_name": last,
        "full_name": full or f"{first} {last}".strip(),
        "title": str(
            r.get("title") or r.get("job_title") or r.get("owner_title") or ""
        ).strip(),
        "email": email,
        "place_id": str(r.get("place_id") or "").strip(),
        "city": str(r.get("city") or r.get("address_city") or "").strip(),
        "state": str(r.get("state") or r.get("address_state") or "").strip(),
        "linkedin_url": str(
            r.get("linkedin_url") or r.get("linkedin") or r.get("profile_url") or ""
        ).strip(),
        "phone": str(
            r.get("phone") or r.get("cellphone") or r.get("mobile") or ""
        ).strip(),
        "ai_ark_id": str(r.get("ai_ark_id") or r.get("person_id") or "").strip(),
        "source": str(r.get("source") or "waterfall").strip() or "waterfall",
        "mode": mode,
        "_source_key": r.get("_source_key"),
    }


def _empty_stats() -> dict[str, int]:
    return {"calls": 0, "email_hits": 0, "dm_hits": 0, "phone_hits": 0, "errors": 0}


def _domain_from_email(email: str) -> str:
    raw = (email or "").strip().lower()
    if "@" not in raw:
        return ""
    return _host_from(raw.split("@", 1)[1])


def _domain_from_raw(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    for key in ("domain", "company_domain", "website", "company_website"):
        host = _host_from(str(raw.get(key) or ""))
        if host:
            return host
    account = raw.get("account") or raw.get("company") or {}
    if isinstance(account, dict):
        return _host_from(str(account.get("domain") or account.get("website") or ""))
    return ""


def _fill_row_domain(row: dict[str, Any], *, email: str = "", raw: Any = None) -> None:
    if row.get("domain"):
        return
    host = _domain_from_email(email) or _domain_from_raw(raw)
    if host:
        row["domain"] = host


def _tiers_for_mode(mode: str, max_tier: str) -> list[str]:
    allowed = [t for t in TIER_ORDER if tier_allowed(t, max_tier)]
    if mode == "name_company":
        return [t for t in allowed if t in NAME_COMPANY_TIERS]
    if mode == "domain":
        return allowed
    return []


def classify_rows(parsed: list[dict[str, Any]]) -> dict[str, int]:
    modes = {"domain": 0, "name_company": 0, "skipped": 0}
    for row in parsed:
        mode = row.get("mode") or ""
        if mode in modes:
            modes[mode] += 1
        else:
            modes["skipped"] += 1
    return modes


def estimate_waterfall(
    parsed: list[dict[str, Any]],
    *,
    max_tier: str,
    need: str,
    client: ClientConfig,
    verify_only: bool = False,
    caps: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Counts + per-vendor credit estimate. No enrichment vendor calls."""
    max_tier_n = normalize_max_tier(max_tier)
    need_norm, need_caps = resolve_need(need) if caps is None else (need, caps)
    modes = classify_rows(parsed)
    if verify_only:
        mode_counts = {k: v for k, v in modes.items() if k != "skipped" and v}
        if modes.get("skipped"):
            mode_counts["skipped"] = modes["skipped"]
        with_phone = sum(1 for r in parsed if (r.get("phone") or "").strip())
        return {
            "estimate_only": True,
            "verify_only": True,
            "rows_in": sum(v for k, v in modes.items() if k != "skipped"),
            "rows_with_phone": with_phone,
            "modes": mode_counts,
            "tiers_by_mode": {},
            "estimate": {},
            "need": need_norm,
            "need_capabilities": sorted(need_caps),
            "suppressed_by_need": {},
            "max_tier": max_tier_n,
            "client_tag": client.tag,
            "companies_table": client.companies_table,
            "contacts_table": client.contacts_table,
            "spend": 0,
        }
    tiers_by_mode = {
        mode: _tiers_for_mode(mode, max_tier_n)
        for mode in ("domain", "name_company")
        if modes.get(mode)
    }
    vendor_rows: dict[str, int] = {t: 0 for t in TIER_ORDER}
    for mode, count in modes.items():
        if mode == "skipped" or not count:
            continue
        for tier in _tiers_for_mode(mode, max_tier_n):
            vendor_rows[tier] += count

    credits: dict[str, Any] = {}
    sl = SmartleadClient(timeout=8)
    if sl.enabled:
        try:
            sl.refresh_credits(force=True)
        except Exception:
            pass
    sl_snap = sl.credit_snapshot() if sl.api_key else {}

    for tier, count in vendor_rows.items():
        if not count and not (tier == "smartlead" and sl_snap):
            continue
        if count and not tier_serves_need(tier, need_norm, need_caps):
            continue
        per_row = credit_per_row(
            tier, need_norm, CREDIT_PER_ATTEMPT.get(tier, 1.0), need_caps
        )
        row = {
            "rows": count,
            "credits_est": round(count * per_row, 2),
            "credits_per_row": per_row,
            "credits_available": None,
        }
        if tier == "aiark":
            row["rate_note"] = (
                "email+phone"
                if per_row == 1.5
                else ("phone-only" if per_row == 0.5 else "email-only")
            )
        if tier == "smartlead":
            row["credits_available"] = sl_snap.get("available")
            row["credits_total"] = sl_snap.get("total")
            row["credits_used"] = sl_snap.get("used")
        if count:
            credits[tier] = row

    mode_counts = {k: v for k, v in modes.items() if k != "skipped" and v}
    if modes.get("skipped"):
        mode_counts["skipped"] = modes["skipped"]
    return {
        "estimate_only": True,
        "rows_in": sum(v for k, v in modes.items() if k != "skipped"),
        "modes": mode_counts,
        "tiers_by_mode": tiers_by_mode,
        "estimate": credits,
        "need": need_norm,
        "need_capabilities": sorted(need_caps),
        "suppressed_by_need": estimate_suppressed(vendor_rows, need_norm, need_caps),
        "max_tier": max_tier_n,
        "client_tag": client.tag,
        "companies_table": client.companies_table,
        "contacts_table": client.contacts_table,
        "spend": 0,
        "verify_only": False,
    }

