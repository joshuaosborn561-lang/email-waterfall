"""AI Ark — people, work-email, AND cellphone lookup (not people-only).

Email path (sync, Clay-compatible v2):
  LinkedIn URL or AI Ark person id → POST /v2/people/export/single
  name + domain (or phone) → People Search → person id → export/single

Cellphone path (sync, Clay-compatible v2):
  LinkedIn URL, or name + domain → POST /v2/people/mobile-phone-finder

Do not use /v1/people/email-finder (async trackId). Do not use AI Ark for
email-to-profile reverse lookup.
"""

from __future__ import annotations

from typing import Any

from email_waterfall import http_client
from email_waterfall.config import settings
from email_waterfall.need import CAP_EMAIL, CAP_PEOPLE, CAP_PHONE, assert_capability

from .base import EmailHit, PersonHit, PhoneHit, split_name
from .errors import record_response_failure
