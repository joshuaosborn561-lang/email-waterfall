"""DM / work-email enrichment waterfall.

Tiers (fixed, no Maps, no website crawl, no Apify):
  getleads → Smartlead → AI Ark → LeadMagic → Prospeo → FullEnrich

Implementation is split under email_waterfall._wf so GitHub MCP can
upload the modules. This module stays the public import surface.
"""

from __future__ import annotations

from . import source as table_source
from . import supabase_sync
from .clients import ensure_client
from .vendors.ai_ark import AiArkClient
from .vendors.fullenrich import FullEnrichClient
from .vendors.getleads import GetLeadsClient
from .vendors.leadmagic import LeadMagicClient
from .vendors.prospeo import ProspeoClient
from .vendors.smartlead import SmartleadClient
from .vendors.veriphone import VeriphoneClient

from ._wf.const import (
    CREDIT_PER_ATTEMPT,
    DEFAULT_MAX_TIER,
    NAME_COMPANY_TIERS,
    Need,
    MaxTier,
    TIER_ORDER,
    TIER_RANK,
    classify_rows,
    estimate_waterfall,
    normalize_max_tier,
    tier_allowed,
    _norm_row,
    _parse_rows,
    _has_inline_rows,
)
from ._wf.engine import Waterfall
from ._wf.exec import enrich_waterfall
from ._wf.one import compact_person_hit, enrich_one_person
from ._wf.row import _enrich_one_row, _write_company_contact

__all__ = [
    "AiArkClient",
    "FullEnrichClient",
    "GetLeadsClient",
    "LeadMagicClient",
    "ProspeoClient",
    "SmartleadClient",
    "VeriphoneClient",
    "Waterfall",
    "classify_rows",
    "compact_person_hit",
    "enrich_one_person",
    "enrich_waterfall",
    "estimate_waterfall",
    "normalize_max_tier",
    "tier_allowed",
    "TIER_ORDER",
    "DEFAULT_MAX_TIER",
    "supabase_sync",
    "table_source",
]
