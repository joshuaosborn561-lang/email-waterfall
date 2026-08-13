"""DM / work-email enrichment waterfall.

Tiers (fixed, no Maps, no website crawl, no Apify):
  getleads → AI Ark → LeadMagic → FullEnrich (email only, opt-in)

AI Ark is second on BOTH lanes:
  people/DM: People Search by domain
  email: LinkedIn URL / person id / name+domain / phone → export/single

Writes to public.{client}_companies / public.{client}_contacts.
"""

from __future__ import annotations

import json
from typing import Any, Literal
from urllib.parse import urlsplit

from . import supabase_sync
from .clients import ClientConfig, get_client, parse_target_titles
from .people import looks_like_person, pick_best_person
from .vendors.ai_ark import AiArkClient
from .vendors.base import EmailHit, PersonHit, split_name
from .vendors.fullenrich import FullEnrichClient
from .vendors.getleads import GetLeadsClient
from .vendors.leadmagic import LeadMagicClient

Need = Literal["email", "dm", "both"]
MaxTier = Literal["getleads", "aiark", "leadmagic", "fullenrich"]

TIER_ORDER: list[str] = ["getleads", "aiark", "leadmagic", "fullenrich"]
TIER_RANK = {name: i for i, name in enumerate(TIER_ORDER)}
DEFAULT_MAX_TIER: MaxTier = "leadmagic"


def normalize_max_tier(max_tier: str | None) -> str:
    t = (max_tier or DEFAULT_MAX_TIER).strip().lower()
    aliases = {
        "ai_ark": "aiark",
        "ai-ark": "aiark",
        "full_enrich": "fullenrich",
        "full-enrich": "fullenrich",
        "get_leads": "getleads",
        "lead_magic": "leadmagic",
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
    return {
        "domain": domain,
        "company_name": str(
            r.get("company_name") or r.get("company") or r.get("business_name") or ""
        ).strip(),
        "first_name": first,
        "last_name": last,
        "full_name": full or f"{first} {last}".strip(),
        "title": str(
            r.get("title") or r.get("job_title") or r.get("owner_title") or ""
        ).strip(),
        "email": str(r.get("email") or "").strip().lower(),
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
    }


def _empty_stats() -> dict[str, int]:
    return {"calls": 0, "email_hits": 0, "dm_hits": 0}


class Waterfall:
    def __init__(
        self,
        *,
        getleads: GetLeadsClient | None = None,
        ai_ark: AiArkClient | None = None,
        leadmagic: LeadMagicClient | None = None,
        fullenrich: FullEnrichClient | None = None,
        max_tier: str = DEFAULT_MAX_TIER,
        target_titles: list[str] | None = None,
        fallback_titles: frozenset[str] | None = None,
        require_title_match: bool = True,
    ):
        self.getleads = getleads or GetLeadsClient()
        self.ai_ark = ai_ark or AiArkClient()
        self.leadmagic = leadmagic or LeadMagicClient()
        self.fullenrich = fullenrich or FullEnrichClient()
        self.max_tier = normalize_max_tier(max_tier)
        self.target_titles = list(target_titles or [])
        self.fallback_titles = fallback_titles or frozenset()
        self.require_title_match = bool(require_title_match)
        self.tier_stats = {
            "aiark": _empty_stats(),
            "getleads": _empty_stats(),
            "leadmagic": _empty_stats(),
            "fullenrich": _empty_stats(),
        }
        self._vendor_dm_cache: dict[str, PersonHit | None] = {}
        self._email_cache: dict[tuple[str, ...], EmailHit | None] = {}

    def _bump(self, tier: str, field: str) -> None:
        self.tier_stats.setdefault(tier, _empty_stats())
        self.tier_stats[tier][field] = self.tier_stats[tier].get(field, 0) + 1

    def _allowed(self, tier: str) -> bool:
        return tier_allowed(tier, self.max_tier)

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
            (row.get("linkedin_url") or "").lower(),
            (row.get("phone") or "").strip(),
            str(row.get("ai_ark_id") or ""),
        )

    def resolve_email(
        self, row: dict[str, Any], *, include_fullenrich: bool = True
    ) -> EmailHit | None:
        first, last, domain = row["first_name"], row["last_name"], row["domain"]
        if row.get("email"):
            return EmailHit(email=row["email"], source_tier="input", status="provided")
        linkedin = row.get("linkedin_url") or ""
        phone = row.get("phone") or ""
        person_id = str(row.get("ai_ark_id") or "")
        has_name_domain = bool(first and last and domain)
        can_aiark = bool(
            linkedin or person_id or has_name_domain or (phone and (first or last or domain))
        )
        if not has_name_domain and not can_aiark:
            return None
        cache_key = self._email_cache_key(row)
        if cache_key in self._email_cache:
            return self._email_cache[cache_key]

        hit: EmailHit | None = None
        company = row.get("company_name") or ""

        if has_name_domain and self.getleads.enabled and self._allowed("getleads"):
            self._bump("getleads", "calls")
            hit = self.getleads.find_email(first, last, domain, company)
            if hit:
                self._bump("getleads", "email_hits")

        if not hit and can_aiark and self.ai_ark.enabled and self._allowed("aiark"):
            self._bump("aiark", "calls")
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
            if hit:
                self._bump("aiark", "email_hits")

        if (
            not hit
            and has_name_domain
            and self.leadmagic.enabled
            and self._allowed("leadmagic")
        ):
            self._bump("leadmagic", "calls")
            hit = self.leadmagic.find_email(first, last, domain, company)
            if hit:
                self._bump("leadmagic", "email_hits")

        if (
            not hit
            and include_fullenrich
            and has_name_domain
            and self.fullenrich.enabled
            and self._allowed("fullenrich")
        ):
            self._bump("fullenrich", "calls")
            hit = self.fullenrich.find_email(first, last, domain, company)
            if hit:
                self._bump("fullenrich", "email_hits")

        self._email_cache[cache_key] = hit
        return hit

    def resolve_dm(self, row: dict[str, Any]) -> PersonHit | None:
        domain = row["domain"]
        if not domain:
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

        if domain in self._vendor_dm_cache:
            consider(self._vendor_dm_cache[domain])
            return best

        vendors: list[tuple[str, Any]] = []
        if self.getleads.enabled and self._allowed("getleads"):
            vendors.append(("getleads", self.getleads))
        if self.ai_ark.enabled and self._allowed("aiark"):
            vendors.append(("aiark", self.ai_ark))
        if self.leadmagic.enabled and self._allowed("leadmagic"):
            vendors.append(("leadmagic", self.leadmagic))

        vendor_best: PersonHit | None = None
        vendor_rank = -1
        for tier, client in vendors:
            self._bump(tier, "calls")
            people = client.find_people(
                domain,
                company_name=row.get("company_name") or "",
                titles=self.target_titles,
            )
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

        self._vendor_dm_cache[domain] = vendor_best
        return best


def enrich_waterfall(
    rows: Any,
    *,
    client_tag: str,
    need: Need = "both",
    max_tier: str = DEFAULT_MAX_TIER,
    target_titles: str | list[str] | None = "",
    require_title_match: bool = True,
    write_supabase: bool = True,
) -> dict[str, Any]:
    """Walk paid vendors per row; upsert isolated client tables; return counts."""
    client = get_client(client_tag)
    max_tier_n = normalize_max_tier(max_tier)
    need_norm = (need or "both").strip().lower()
    if need_norm not in ("email", "dm", "both"):
        raise ValueError("need must be 'email', 'dm', or 'both'")

    if isinstance(target_titles, list):
        titles = [str(t).strip() for t in target_titles if str(t).strip()]
        titles = titles or list(client.titles)
    else:
        titles = parse_target_titles(target_titles, client)

    parsed = [_norm_row(r) for r in _parse_rows(rows)]
    parsed = [r for r in parsed if r.get("domain")]
    if not parsed:
        return {
            "rows_in": 0,
            "companies_upserted": 0,
            "contacts_written": 0,
            "emails_found": 0,
            "dms_found": 0,
            "tier_stats": {},
            "need": need_norm,
            "max_tier": max_tier_n,
            "client_tag": client.tag,
            "companies_table": client.companies_table,
            "contacts_table": client.contacts_table,
            "target_titles": titles,
            "require_title_match": bool(require_title_match),
        }

    wf = Waterfall(
        max_tier=max_tier_n,
        target_titles=titles,
        fallback_titles=client.fallback_titles,
        require_title_match=bool(require_title_match),
    )

    pending_fe: list[tuple[int, dict[str, Any]]] = []
    enriched: list[dict[str, Any]] = []

    for idx, row in enumerate(parsed):
        email = row.get("email") or ""
        email_tier = "input" if email else ""
        person: PersonHit | None = None
        dm_tier = ""

        if need_norm in ("dm", "both"):
            person = wf.resolve_dm(row)
            if person:
                dm_tier = person.source_tier
                if not row["first_name"] and person.first_name:
                    row["first_name"] = person.first_name
                    row["last_name"] = person.last_name
                    row["full_name"] = person.name
                if not row.get("title") and person.title:
                    row["title"] = person.title
                if person.linkedin_url and not row.get("linkedin_url"):
                    row["linkedin_url"] = person.linkedin_url
                if person.phone and not row.get("phone"):
                    row["phone"] = person.phone
                if person.source_tier == "aiark":
                    pid = str((person.raw or {}).get("id") or "").strip()
                    if pid and not row.get("ai_ark_id"):
                        row["ai_ark_id"] = pid
                if person.email and not email:
                    email = person.email
                    email_tier = person.source_tier

        if need_norm in ("email", "both") and not email:
            hit = wf.resolve_email(row, include_fullenrich=False)
            if hit:
                email, email_tier = hit.email, hit.source_tier
            elif (
                row["first_name"]
                and row["last_name"]
                and row["domain"]
                and wf.fullenrich.enabled
                and wf._allowed("fullenrich")
            ):
                pending_fe.append((idx, row))

        enriched.append(
            {
                "row": row,
                "email": email,
                "email_tier": email_tier,
                "dm_tier": dm_tier,
                "person": person,
            }
        )

    if pending_fe and wf.fullenrich.enabled and wf._allowed("fullenrich"):
        fe_rows = [
            {
                "first_name": r["first_name"],
                "last_name": r["last_name"],
                "domain": r["domain"],
                "company_name": r.get("company_name") or "",
            }
            for _, r in pending_fe
        ]
        wf._bump("fullenrich", "calls")
        hits = wf.fullenrich.find_email_bulk(fe_rows)
        for (idx, _row), hit in zip(pending_fe, hits):
            if hit:
                wf._bump("fullenrich", "email_hits")
                enriched[idx]["email"] = hit.email
                enriched[idx]["email_tier"] = hit.source_tier
    elif pending_fe:
        wf.tier_stats["fullenrich"]["blocked_by_max_tier"] = len(pending_fe)

    company_rows: list[dict[str, Any]] = []
    contact_rows: list[dict[str, Any]] = []
    emails_found = 0
    dms_found = 0

    for item in enriched:
        row = item["row"]
        email = item["email"]
        email_tier = item["email_tier"]
        dm_tier = item["dm_tier"]
        person = item["person"]
        if email:
            emails_found += 1
        if dm_tier:
            dms_found += 1

        company_rows.append(
            supabase_sync.company_row(
                client_tag=client.tag,
                domain=row["domain"],
                company_name=row.get("company_name") or "",
                source=row.get("source") or "waterfall",
                place=row.get("place_id") or "",
                address_city=row.get("city") or "",
                address_state=row.get("state") or "",
                email_source_tier=email_tier if email_tier != "input" else email_tier,
                dm_source_tier=dm_tier,
                source_tier={
                    k: v
                    for k, v in {"email": email_tier, "dm": dm_tier}.items()
                    if v
                },
                dm_lookup_status="found" if dm_tier else "not_found",
            )
        )

        first = row["first_name"]
        last = row["last_name"]
        title = row.get("title") or ""
        if person:
            first = person.first_name or first
            last = person.last_name or last
            title = person.title or title
        if first or last or email:
            contact_rows.append(
                supabase_sync.contact_row(
                    client_tag=client.tag,
                    domain=row["domain"],
                    first_name=first,
                    last_name=last,
                    job_title=title,
                    email=email,
                    email_status="found" if email else "",
                    linkedin_url=(person.linkedin_url if person else "")
                    or row.get("linkedin_url")
                    or "",
                    cellphone=(person.phone if person else "")
                    or row.get("phone")
                    or "",
                    contact_city=row.get("city") or "",
                    contact_state=row.get("state") or "",
                    source_tool=email_tier or dm_tier or "waterfall",
                    source_tier=email_tier or dm_tier,
                    place_id=row.get("place_id") or "",
                    confidence=0.7 if email or dm_tier else 0.0,
                )
            )

    companies_upserted = 0
    contacts_written = 0
    if write_supabase:
        companies_upserted = supabase_sync.upsert_companies(client, company_rows)
        with_email = [r for r in contact_rows if r.get("email")]
        no_email = [r for r in contact_rows if not r.get("email")]
        contacts_written = supabase_sync.insert_contacts_ignore_conflict(
            client, with_email
        )
        contacts_written += supabase_sync.insert_contacts(client, no_email)

    for name, vendor in (
        ("getleads", wf.getleads),
        ("aiark", wf.ai_ark),
        ("leadmagic", wf.leadmagic),
        ("fullenrich", wf.fullenrich),
    ):
        wf.tier_stats[name]["vendor_calls"] = getattr(vendor, "calls", 0)
        wf.tier_stats[name]["vendor_hits"] = getattr(vendor, "hits", 0)

    tier_breakdown = {}
    for tier_name, stats in wf.tier_stats.items():
        allowed = tier_allowed(tier_name, max_tier_n) if tier_name in TIER_RANK else True
        tier_breakdown[tier_name] = {
            "attempts": int(stats.get("calls") or 0),
            "email_hits": int(stats.get("email_hits") or 0),
            "dm_hits": int(stats.get("dm_hits") or 0),
            "vendor_calls": int(stats.get("vendor_calls") or 0),
            "vendor_hits": int(stats.get("vendor_hits") or 0),
            "allowed_by_max_tier": allowed,
            "estimated_cost_usd": 0.0,
        }

    return {
        "rows_in": len(parsed),
        "companies_upserted": companies_upserted,
        "contacts_written": contacts_written,
        "emails_found": emails_found,
        "dms_found": dms_found,
        "tier_stats": wf.tier_stats,
        "tier_breakdown": tier_breakdown,
        "need": need_norm,
        "max_tier": max_tier_n,
        "client_tag": client.tag,
        "companies_table": client.companies_table,
        "contacts_table": client.contacts_table,
        "target_titles": titles,
        "require_title_match": bool(require_title_match),
        "vendors_enabled": {
            "getleads": wf.getleads.enabled and wf._allowed("getleads"),
            "aiark": wf.ai_ark.enabled and wf._allowed("aiark"),
            "leadmagic": wf.leadmagic.enabled and wf._allowed("leadmagic"),
            "fullenrich": wf.fullenrich.enabled and wf._allowed("fullenrich"),
        },
    }
