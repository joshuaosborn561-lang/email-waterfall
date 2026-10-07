"""DM / work-email enrichment waterfall.

Tiers (fixed, no Maps, no website crawl, no Apify):
  getleads → Smartlead (plan email finder) → AI Ark → LeadMagic → Prospeo → FullEnrich

AI Ark is third on BOTH lanes after the included Smartlead allotment is used:
  people/DM: People Search by domain
  email: LinkedIn URL / person id / name+domain / phone → export/single

Also fills cellphone: AI Ark mobile-phone-finder (LinkedIn or name+domain),
then LeadMagic mobile-finder, then Prospeo if max_tier allows.

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
NAME_COMPANY_TIERS: tuple[str, ...] = ("aiark", "leadmagic", "prospeo", "fullenrich")
CREDIT_PER_ATTEMPT: dict[str, float] = {
    "getleads": 1.0,
    "smartlead": 1.0,
    "aiark": 1.5,
    "leadmagic": 1.0,
    "prospeo": 1.0,
    "fullenrich": 1.0,
}
