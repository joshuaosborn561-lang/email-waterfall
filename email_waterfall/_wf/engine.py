from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger("email_waterfall.waterfall")

from email_waterfall.need import (
    CAP_EMAIL,
    CAP_PEOPLE,
    CAP_PHONE,
    assert_capability,
    capabilities,
    normalize_need,
    set_need,
)
from email_waterfall.people import looks_like_person, pick_best_person
from email_waterfall.vendors.base import EmailHit, PersonHit, PhoneHit
from email_waterfall.vendors.veriphone import VeriphoneResult

from email_waterfall._wf.const import (
    CIRCUIT_ERROR_LIMIT,
    CIRCUIT_WINDOW,
    DEFAULT_MAX_TIER,
    _empty_stats,
    _fill_row_domain,
    normalize_max_tier,
    tier_allowed,
)


def _host():
    from email_waterfall import waterfall as w
    return w


def _int_attr(obj: Any, name: str) -> int:
    val = getattr(obj, name, 0)
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


class Waterfall:
    def __init__(
        self,
        *,
        getleads: Any = None,
        smartlead: Any = None,
        ai_ark: Any = None,
        leadmagic: Any = None,
        prospeo: Any = None,
        fullenrich: Any = None,
        veriphone: Any = None,
        max_tier: str = DEFAULT_MAX_TIER,
        target_titles: list[str] | None = None,
        fallback_titles: frozenset[str] | None = None,
        require_title_match: bool = True,
        need: str = "both",
        verify_only: bool = False,
        caps: frozenset[str] | None = None,
    ):
        h = _host()
        self.getleads = getleads or h.GetLeadsClient()
        self.smartlead = smartlead or h.SmartleadClient()
        self.ai_ark = ai_ark or h.AiArkClient()
        self.leadmagic = leadmagic or h.LeadMagicClient()
        self.prospeo = prospeo or h.ProspeoClient()
        self.fullenrich = fullenrich or h.FullEnrichClient()
        self.veriphone = veriphone or h.VeriphoneClient()
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

    def _phone_vendor_eligible(
        self, row: dict[str, Any], *, email: str = ""
    ) -> dict[str, bool]:
        existing = (row.get("phone") or "").strip()
        if existing:
            return {}
        linkedin = (row.get("linkedin_url") or "").strip()
        work_email = (email or row.get("email") or "").strip().lower()
        first = row.get("first_name") or ""
        last = row.get("last_name") or ""
        domain = row.get("domain") or ""
        full = (row.get("full_name") or f"{first} {last}".strip()).strip()
        can_aiark = bool(linkedin or (domain and full))
        can_lm = bool(linkedin or work_email)
        can_prospeo = bool(linkedin or (first and last and domain))
        can_fe = bool(
            (first and last and (domain or row.get("company_name") or linkedin))
        )
        return {
            "aiark": can_aiark and self.ai_ark.enabled and self._allowed("aiark"),
            "leadmagic": can_lm and self.leadmagic.enabled and self._allowed("leadmagic"),
            "prospeo": can_prospeo
            and self.prospeo.enabled
            and self._allowed("prospeo"),
            "fullenrich": can_fe
            and self.fullenrich.enabled
            and self._allowed("fullenrich"),
        }

    def record_phone_skips(self, row: dict[str, Any], *, email: str = "") -> None:
        """Count phone lookups we refused to buy because need forbids phone."""
        for tier, eligible in self._phone_vendor_eligible(row, email=email).items():
            if eligible:
                self._suppress(f"{tier}_phone")

    def _phone_only(self) -> bool:
        return CAP_PHONE in self.caps and CAP_EMAIL not in self.caps

    def _should_verify_phone(self) -> bool:
        return self.verify_only or self._phone_only()

    def cached_phone_verdict(self, number: str) -> VeriphoneResult | None:
        digits = self._phone_digits(number)
        if not digits:
            return None
        with self._lock:
            return self._veriphone_cache.get(digits)

    def _phone_verdict(self, number: str) -> VeriphoneResult | None:
        """GET /v2/verify, cached by digits. None if unconfigured or HTTP failed."""
        raw = (number or "").strip()
        if not raw:
            return None
        digits = self._phone_digits(raw)
        if digits:
            with self._lock:
                if digits in self._veriphone_cache:
                    return self._veriphone_cache[digits]
        if not self.veriphone.enabled:
            return None
        assert_capability(CAP_PHONE, vendor="waterfall", endpoint="veriphone")
        result = self.veriphone.verify(raw)
        if digits:
            with self._lock:
                self._veriphone_cache[digits] = result
        return result

    def _accept_phone(
        self,
        phone: str,
        *,
        source_tier: str,
        raw: dict[str, Any] | None = None,
    ) -> PhoneHit | None:
        """On need='phone' (or verify_only), keep the number only when mobile."""
        number = (phone or "").strip()
        if not number:
            return None
        if not self._should_verify_phone():
            return PhoneHit(phone=number, source_tier=source_tier, raw=raw or {})

        if not self.veriphone.enabled:
            self._bump("veriphone", "skipped_unconfigured")
            return PhoneHit(phone=number, source_tier=source_tier, raw=raw or {})

        result = self._phone_verdict(number)
        if result is not None and result.is_mobile:
            self._bump("veriphone", "phone_hits")
            merged = dict(raw or {})
            merged["veriphone"] = result.raw
            return PhoneHit(
                phone=result.e164 or result.phone,
                source_tier=source_tier,
                raw=merged,
            )
        self._bump("veriphone", "rejected")
        return None

    def resolve_phone(
        self, row: dict[str, Any], *, email: str = ""
    ) -> PhoneHit | None:
        self._bind_need()
        existing = (row.get("phone") or "").strip()
        if existing:
            accepted = self._accept_phone(existing, source_tier="input")
            if accepted:
                return accepted
            if not self._phone_only():
                return PhoneHit(phone=existing, source_tier="input")
            # Landline / non-mobile input: keep looking for a vendor mobile.

        # Existing phone already handled above. Do not skip finders just
        # because a rejected landline is still on the row.
        eligible = self._phone_vendor_eligible({**row, "phone": ""}, email=email)
        if not any(eligible.values()):
            return None
        assert_capability(CAP_PHONE, vendor="waterfall", endpoint="resolve_phone")

        linkedin = (row.get("linkedin_url") or "").strip()
        work_email = (email or row.get("email") or "").strip().lower()
        first = row.get("first_name") or ""
        last = row.get("last_name") or ""
        domain = row.get("domain") or ""
        full = (row.get("full_name") or f"{first} {last}".strip()).strip()

        cache_key = (
            linkedin.lower(),
            work_email,
            domain.lower(),
            first.lower(),
            last.lower(),
        )
        with self._lock:
            if cache_key in self._phone_cache:
                return self._phone_cache[cache_key]

        hit: PhoneHit | None = None
        if eligible.get("aiark"):
            before_e = _int_attr(self.ai_ark, "errors")
            self._bump_attempt("aiark", row)
            found = self.ai_ark.find_mobile(
                first,
                last,
                domain,
                row.get("company_name") or "",
                linkedin_url=linkedin,
                full_name=full,
            )
            self._note_circuit("aiark", _int_attr(self.ai_ark, "errors") > before_e)
            if found:
                hit = self._accept_phone(
                    found.phone, source_tier=found.source_tier, raw=found.raw
                )
                if hit:
                    self._bump("aiark", "phone_hits")

        if not hit and eligible.get("leadmagic"):
            before_e = _int_attr(self.leadmagic, "errors")
            self._bump_attempt("leadmagic", row)
            found = self.leadmagic.find_mobile(
                linkedin_url=linkedin, work_email=work_email
            )
            self._note_circuit(
                "leadmagic", _int_attr(self.leadmagic, "errors") > before_e
            )
            if found:
                hit = self._accept_phone(
                    found.phone, source_tier=found.source_tier, raw=found.raw
                )
                if hit:
                    self._bump("leadmagic", "phone_hits")

        if not hit and eligible.get("prospeo"):
            before_e = _int_attr(self.prospeo, "errors")
            self._bump_attempt("prospeo", row)
            found = self.prospeo.find_mobile(
                first,
                last,
                domain,
                row.get("company_name") or "",
                linkedin_url=linkedin,
                full_name=full,
            )
            self._note_circuit(
                "prospeo", _int_attr(self.prospeo, "errors") > before_e
            )
            if found:
                hit = self._accept_phone(
                    found.phone, source_tier=found.source_tier, raw=found.raw
                )
                if hit:
                    self._bump("prospeo", "phone_hits")

        if not hit and eligible.get("fullenrich"):
            before_e = _int_attr(self.fullenrich, "errors")
            self._bump_attempt("fullenrich", row)
            found = self.fullenrich.find_mobile(
                first,
                last,
                domain,
                row.get("company_name") or "",
                linkedin_url=linkedin,
            )
            self._note_circuit(
                "fullenrich", _int_attr(self.fullenrich, "errors") > before_e
            )
            if found:
                hit = self._accept_phone(
                    found.phone, source_tier=found.source_tier, raw=found.raw
                )
                if hit:
                    self._bump("fullenrich", "phone_hits")

        with self._lock:
            self._phone_cache[cache_key] = hit
        return hit


