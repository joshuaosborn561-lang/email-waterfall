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

from . import source as table_source
from . import supabase_sync
from .clients import ClientConfig, ensure_client, parse_target_titles
from .concurrency import company_concurrency
from .need import (
    CAP_EMAIL,
    CAP_PEOPLE,
    CAP_PHONE,
    allows,
    assert_capability,
    capabilities,
    credit_per_row,
    estimate_suppressed,
    normalize_need,
    resolve_need,
    set_need,
    tier_serves_need,
    using_need,
)
from .people import looks_like_person, pick_best_person
from .vendors.ai_ark import AiArkClient
from .vendors.base import EmailHit, PersonHit, PhoneHit, split_name
from .vendors.fullenrich import FullEnrichClient
from .vendors.getleads import GetLeadsClient
from .vendors.leadmagic import LeadMagicClient
from .vendors.prospeo import ProspeoClient
from .vendors.smartlead import SmartleadClient
from .vendors.veriphone import VeriphoneClient, VeriphoneResult

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


class Waterfall:
    def __init__(
        self,
        *,
        getleads: GetLeadsClient | None = None,
        smartlead: SmartleadClient | None = None,
        ai_ark: AiArkClient | None = None,
        leadmagic: LeadMagicClient | None = None,
        prospeo: ProspeoClient | None = None,
        fullenrich: FullEnrichClient | None = None,
        veriphone: VeriphoneClient | None = None,
        max_tier: str = DEFAULT_MAX_TIER,
        target_titles: list[str] | None = None,
        fallback_titles: frozenset[str] | None = None,
        require_title_match: bool = True,
        need: str = "both",
        verify_only: bool = False,
        caps: frozenset[str] | None = None,
    ):
        self.getleads = getleads or GetLeadsClient()
        self.smartlead = smartlead or SmartleadClient()
        self.ai_ark = ai_ark or AiArkClient()
        self.leadmagic = leadmagic or LeadMagicClient()
        self.prospeo = prospeo or ProspeoClient()
        self.fullenrich = fullenrich or FullEnrichClient()
        self.veriphone = veriphone or VeriphoneClient()
        self.max_tier = normalize_max_tier(max_tier)
        if caps is None:
            self.need = normalize_need(need)
            self.caps = capabilities(self.need)
        else:
            self.need = need
            self.caps = caps
        self.verify_only = bool(verify_only)
        self.target_titles = list(target_titles or [])
        self.fallback_titles = fallback_titles or frozenset()
        self.require_title_match = bool(require_title_match)
        self.tier_stats = {
            "aiark": _empty_stats(),
            "getleads": _empty_stats(),
            "smartlead": _empty_stats(),
            "leadmagic": _empty_stats(),
            "prospeo": _empty_stats(),
            "fullenrich": _empty_stats(),
            "veriphone": _empty_stats(),
        }
        self.suppressed_by_need: dict[str, int] = {}
        self._row_attempts: set[tuple[str, int]] = set()
        self._vendor_dm_cache: dict[str, PersonHit | None] = {}
        self._email_cache: dict[tuple[str, ...], EmailHit | None] = {}
        self._phone_cache: dict[tuple[str, ...], PhoneHit | None] = {}
        self._veriphone_cache: dict[str, VeriphoneResult | None] = {}
        self._circuit_outcomes: dict[str, list[bool]] = {}
        self._circuit_disabled: dict[str, str] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _phone_digits(number: str) -> str:
        return "".join(c for c in (number or "") if c.isdigit())

    def _bump(self, tier: str, field: str) -> None:
        with self._lock:
            self.tier_stats.setdefault(tier, _empty_stats())
            self.tier_stats[tier][field] = self.tier_stats[tier].get(field, 0) + 1

    def _bump_attempt(self, tier: str, row: dict[str, Any]) -> None:
        """One attempt per row per tier. Extra HTTP is vendor_calls, not attempts.

        AI Ark name+domain email is People Search then export/single: 1 attempt,
        2 vendor_calls. Email + phone on the same row is still 1 attempt.
        """
        key = (tier, id(row))
        with self._lock:
            if key in self._row_attempts:
                return
            self._row_attempts.add(key)
            self.tier_stats.setdefault(tier, _empty_stats())
            self.tier_stats[tier]["calls"] = self.tier_stats[tier].get("calls", 0) + 1

    def _suppress(self, key: str) -> None:
        with self._lock:
            self.suppressed_by_need[key] = self.suppressed_by_need.get(key, 0) + 1

    def _bind_need(self) -> None:
        set_need(self.need, self.caps)

    def _allowed(self, tier: str) -> bool:
        if tier in self._circuit_disabled:
            return False
        return tier_allowed(tier, self.max_tier)

    def _note_circuit(self, tier: str, errored: bool) -> None:
        """Disable a tier after more than 20 errors in its first 25 calls."""
        with self._lock:
            if tier in self._circuit_disabled:
                return
            outcomes = self._circuit_outcomes.setdefault(tier, [])
            if len(outcomes) >= CIRCUIT_WINDOW:
                return
            outcomes.append(bool(errored))
            errors = sum(1 for flag in outcomes if flag)
            if errors > CIRCUIT_ERROR_LIMIT:
                reason = f"circuit open: {errors} errors in first {len(outcomes)} calls"
                self._circuit_disabled[tier] = reason
                self.tier_stats.setdefault(tier, _empty_stats())
                self.tier_stats[tier]["disabled"] = True
                self.tier_stats[tier]["disabled_reason"] = reason
                log.warning("tier disabled tier=%s reason=%s", tier, reason)

    def _pick(self, people: list[PersonHit]) -> PersonHit | None:
        ranked = pick_best_person(
            people,
            targets=self.target_titles,
            fallback_titles=self.fallback_titles,
            require_title_match=self.require_title_match,
        )
        return ranked.person if ranked else None

    def _email_cache_key(self, row: dict[str, Any]) -> tuple[str, ...]:
        return (
            (row.get("first_name") or "").lower(),
            (row.get("last_name") or "").lower(),
            row.get("domain") or "",
            (row.get("company_name") or "").lower(),
            (row.get("linkedin_url") or "").lower(),
            (row.get("phone") or "").strip(),
            str(row.get("ai_ark_id") or ""),
        )

    def resolve_email(
        self, row: dict[str, Any], *, include_fullenrich: bool = True
    ) -> EmailHit | None:
        self._bind_need()
        first, last, domain = row["first_name"], row["last_name"], row["domain"]
        if row.get("email"):
            return EmailHit(email=row["email"], source_tier="input", status="provided")
        linkedin = row.get("linkedin_url") or ""
        phone = row.get("phone") or ""
        person_id = str(row.get("ai_ark_id") or "")
        company = row.get("company_name") or ""
        has_name_domain = bool(first and last and domain)
        has_name_company = bool(first and last and company)
        can_aiark = bool(
            linkedin
            or person_id
            or has_name_domain
            or has_name_company
            or (phone and (first or last or domain or company))
        )
        if not has_name_domain and not can_aiark and not has_name_company:
            return None
        assert_capability(CAP_EMAIL, vendor="waterfall", endpoint="resolve_email")
        cache_key = self._email_cache_key(row)
        with self._lock:
            if cache_key in self._email_cache:
                return self._email_cache[cache_key]

        hit: EmailHit | None = None

        can_getleads = bool(linkedin or (first and last and (domain or company)))
        if can_getleads and self.getleads.enabled and self._allowed("getleads"):
            before = _int_attr(self.getleads, "calls")
            before_e = _int_attr(self.getleads, "errors")
            hit = self.getleads.find_email(
                first, last, domain, company, linkedin_url=linkedin
            )
            if _int_attr(self.getleads, "calls") > before:
                self._bump_attempt("getleads", row)
                self._note_circuit(
                    "getleads", _int_attr(self.getleads, "errors") > before_e
                )
            if hit:
                self._bump("getleads", "email_hits")
                _fill_row_domain(row, email=hit.email, raw=hit.raw)
                domain = row.get("domain") or domain

        if (
            not hit
            and has_name_domain
            and self.smartlead.enabled
            and self._allowed("smartlead")
        ):
            before_e = _int_attr(self.smartlead, "errors")
            self._bump_attempt("smartlead", row)
            hit = self.smartlead.find_email(first, last, domain, company)
            self._note_circuit(
                "smartlead", _int_attr(self.smartlead, "errors") > before_e
            )
            if hit:
                self._bump("smartlead", "email_hits")

        if not hit and can_aiark and self.ai_ark.enabled and self._allowed("aiark"):
            before_e = _int_attr(self.ai_ark, "errors")
            self._bump_attempt("aiark", row)
            hit = self.ai_ark.find_email(
                first,
                last,
                domain,
                company,
                linkedin_url=linkedin,
                phone=phone,
                person_id=person_id,
                full_name=row.get("full_name") or "",
            )
            self._note_circuit("aiark", _int_attr(self.ai_ark, "errors") > before_e)
            if hit:
                self._bump("aiark", "email_hits")
                _fill_row_domain(row, email=hit.email, raw=hit.raw)
                domain = row.get("domain") or domain

        if (
            not hit
            and (has_name_domain or has_name_company)
            and self.leadmagic.enabled
            and self._allowed("leadmagic")
        ):
            before_e = _int_attr(self.leadmagic, "errors")
            self._bump_attempt("leadmagic", row)
            hit = self.leadmagic.find_email(first, last, domain, company)
            self._note_circuit(
                "leadmagic", _int_attr(self.leadmagic, "errors") > before_e
            )
            if hit:
                self._bump("leadmagic", "email_hits")
                _fill_row_domain(row, email=hit.email, raw=hit.raw)
                domain = row.get("domain") or domain

        can_prospeo = bool(linkedin or has_name_domain or has_name_company)
        if not hit and can_prospeo and self.prospeo.enabled and self._allowed("prospeo"):
            before_e = _int_attr(self.prospeo, "errors")
            self._bump_attempt("prospeo", row)
            hit = self.prospeo.find_email(
                first,
                last,
                domain,
                company,
                linkedin_url=linkedin,
                full_name=row.get("full_name") or "",
            )
            self._note_circuit(
                "prospeo", _int_attr(self.prospeo, "errors") > before_e
            )
            if hit:
                self._bump("prospeo", "email_hits")
                _fill_row_domain(row, email=hit.email, raw=hit.raw)
                domain = row.get("domain") or domain

        if (
            not hit
            and include_fullenrich
            and (has_name_domain or has_name_company)
            and self.fullenrich.enabled
            and self._allowed("fullenrich")
        ):
            before_e = _int_attr(self.fullenrich, "errors")
            self._bump_attempt("fullenrich", row)
            hit = self.fullenrich.find_email(first, last, domain, company)
            self._note_circuit(
                "fullenrich", _int_attr(self.fullenrich, "errors") > before_e
            )
            if hit:
                self._bump("fullenrich", "email_hits")
                _fill_row_domain(row, email=hit.email, raw=hit.raw)

        with self._lock:
            self._email_cache[cache_key] = hit
        return hit

    def resolve_dm(self, row: dict[str, Any]) -> PersonHit | None:
        self._bind_need()
        domain = row["domain"]
        company = row.get("company_name") or ""
        if not domain and not (row.get("mode") == "name_company" and company):
            return None

        best: PersonHit | None = None
        best_rank = -1

        def consider(person: PersonHit | None) -> None:
            nonlocal best, best_rank
            if not person:
                return
            ranked = pick_best_person(
                [person],
                targets=self.target_titles,
                fallback_titles=self.fallback_titles,
                require_title_match=self.require_title_match,
            )
            if not ranked:
                return
            if ranked.rank > best_rank:
                best = ranked.person
                best_rank = ranked.rank

        if row.get("full_name") and looks_like_person(row["first_name"], row["last_name"]):
            consider(
                PersonHit(
                    first_name=row["first_name"],
                    last_name=row["last_name"],
                    full_name=row["full_name"],
                    title=row.get("title") or "",
                    email=row.get("email") or "",
                    linkedin_url=row.get("linkedin_url") or "",
                    source_tier="input",
                )
            )

        def primary_found() -> bool:
            if best is None:
                return False
            ranked = pick_best_person(
                [best],
                targets=self.target_titles,
                fallback_titles=self.fallback_titles,
                require_title_match=False,
            )
            return bool(ranked and ranked.rank > 0 and not ranked.is_fallback)

        if primary_found():
            return best

        cache_key = domain or f"name_company:{company.lower()}"
        with self._lock:
            if cache_key in self._vendor_dm_cache:
                consider(self._vendor_dm_cache[cache_key])
                return best

        vendors: list[tuple[str, Any]] = []
        if domain and self.getleads.enabled and self._allowed("getleads"):
            vendors.append(("getleads", self.getleads))
        if self.ai_ark.enabled and self._allowed("aiark"):
            vendors.append(("aiark", self.ai_ark))
        if domain and self.leadmagic.enabled and self._allowed("leadmagic"):
            vendors.append(("leadmagic", self.leadmagic))

        vendor_best: PersonHit | None = None
        vendor_rank = -1
        if vendors:
            assert_capability(CAP_PEOPLE, vendor="waterfall", endpoint="resolve_dm")
        for tier, client in vendors:
            if not self._allowed(tier):
                continue
            before_e = _int_attr(client, "errors")
            self._bump_attempt(tier, row)
            if domain:
                people = client.find_people(
                    domain,
                    company_name=company,
                    titles=self.target_titles,
                )
            else:
                people = client.find_people(
                    domain,
                    company_name=company,
                    titles=self.target_titles,
                    full_name=row.get("full_name") or "",
                )
            self._note_circuit(tier, _int_attr(client, "errors") > before_e)
            picked = self._pick(people)
            if picked:
                self._bump(tier, "dm_hits")
                consider(picked)
                ranked = pick_best_person(
                    [picked],
                    targets=self.target_titles,
                    fallback_titles=self.fallback_titles,
                    require_title_match=self.require_title_match,
                )
                if ranked and ranked.rank > vendor_rank:
                    vendor_best = ranked.person
                    vendor_rank = ranked.rank
                if primary_found():
                    break

        with self._lock:
            self._vendor_dm_cache[cache_key] = vendor_best
        return best
