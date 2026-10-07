
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
