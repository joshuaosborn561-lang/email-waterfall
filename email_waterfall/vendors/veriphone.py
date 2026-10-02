"""Veriphone — confirm a number is a mobile before we write cellphone.

Used on need='phone' runs only. GET /v2/verify; accept phone_valid and
phone_type=mobile. Landline / voip / unknown are rejects, not writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from email_waterfall import http_client
from email_waterfall.config import settings
from email_waterfall.need import CAP_PHONE, assert_capability

from .base import PhoneHit

MOBILE_TYPES = frozenset({"mobile"})


@dataclass
class VeriphoneResult:
    phone: str
    phone_valid: bool
    phone_type: str
    e164: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_mobile(self) -> bool:
        return bool(self.phone_valid) and self.phone_type.lower() in MOBILE_TYPES


class VeriphoneClient:
    tier = "veriphone"
    base_url = "https://api.veriphone.io"

    def __init__(self, api_key: str | None = None, timeout: int = 20):
        self.api_key = api_key if api_key is not None else settings.veriphone_api_key
        self.timeout = timeout
        self.calls = 0
        self.hits = 0
        self.errors = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def verify(
        self, phone: str, *, default_country: str = "US"
    ) -> VeriphoneResult | None:
        """GET /v2/verify. None if unconfigured, empty, or the HTTP call failed."""
        number = (phone or "").strip()
        if not number or not self.enabled:
            return None
        assert_capability(
            CAP_PHONE, vendor=self.tier, endpoint="GET /v2/verify"
        )
        qs = urlencode({"phone": number, "default_country": default_country})
        url = f"{self.base_url}/v2/verify?{qs}"
        self.calls += 1
        r = http_client.get(
            self.tier,
            url,
            headers=self._headers(),
            timeout=self.timeout,
        )
        if r is None:
            self.errors += 1
            return None
        if r.status_code >= 400:
            self.errors += 1
            return None
        try:
            body = r.json()
        except ValueError:
            self.errors += 1
            return None
        if not isinstance(body, dict):
            self.errors += 1
            return None
        status = str(body.get("status") or "").strip().lower()
        if status in {"error", "syntax-error"} and not body.get("phone_valid"):
            phone_type = str(body.get("phone_type") or "").strip().lower()
            return VeriphoneResult(
                phone=number,
                phone_valid=False,
                phone_type=phone_type,
                e164=str(body.get("e164") or "").strip(),
                raw=body,
            )
        phone_type = str(body.get("phone_type") or "").strip().lower()
        valid = bool(body.get("phone_valid"))
        e164 = str(body.get("e164") or "").strip()
        return VeriphoneResult(
            phone=number,
            phone_valid=valid,
            phone_type=phone_type,
            e164=e164,
            raw=body,
        )

    def check_mobile(
        self, phone: str, *, default_country: str = "US"
    ) -> PhoneHit | None:
        """Return a PhoneHit only when Veriphone says the number is a mobile."""
        result = self.verify(phone, default_country=default_country)
        if result is None or not result.is_mobile:
            return None
        self.hits += 1
        return PhoneHit(
            phone=result.e164 or result.phone,
            source_tier=self.tier,
            raw=result.raw,
        )
