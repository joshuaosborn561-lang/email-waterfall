
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
