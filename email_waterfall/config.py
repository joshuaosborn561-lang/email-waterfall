"""Environment-backed settings. No Maps / Apify / crawl keys."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DEFAULT_SUPABASE_URL = "https://azpapwtnrbzywlnxxecz.supabase.co"
DEFAULT_SUPABASE_PROJECT = "azpapwtnrbzywlnxxecz"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


@dataclass(frozen=True)
class Settings:
    supabase_url: str
    supabase_service_role_key: str
    supabase_anon_key: str
    getleads_mcp_url: str
    getleads_oauth_issuer: str
    getleads_client_id: str
    getleads_refresh_token: str
    getleads_api_key: str
    ai_ark_api_key: str
    leadmagic_api_key: str
    prospeo_api_key: str
    fullenrich_api_key: str
    veriphone_api_key: str
    smartlead_api_key: str
    smartlead_base_url: str

    @property
    def supabase_key(self) -> str:
        return self.supabase_service_role_key or self.supabase_anon_key

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)


def load_settings() -> Settings:
    return Settings(
        supabase_url=_env("SUPABASE_URL", DEFAULT_SUPABASE_URL).rstrip("/"),
        supabase_service_role_key=_env("SUPABASE_SERVICE_ROLE_KEY"),
        supabase_anon_key=_env("SUPABASE_ANON_KEY"),
        getleads_mcp_url=_env(
            "GETLEADS_MCP_URL", "https://app.getleads.io/api/mcp"
        ).rstrip("/"),
        getleads_oauth_issuer=_env(
            "GETLEADS_OAUTH_ISSUER", "https://app.getleads.io"
        ).rstrip("/"),
        getleads_client_id=_env("GETLEADS_CLIENT_ID"),
        getleads_refresh_token=_env("GETLEADS_REFRESH_TOKEN"),
        getleads_api_key=_env("GETLEADS_API_KEY") or _env("GETLEADS_KEY"),
        ai_ark_api_key=_env("AI_ARK_API_KEY") or _env("AIARK_API_KEY"),
        leadmagic_api_key=_env("LEADMAGIC_API_KEY") or _env("LEADMAGIC_KEY"),
        prospeo_api_key=_env("PROSPEO_API_KEY"),
        fullenrich_api_key=_env("FULLENRICH_API_KEY"),
        veriphone_api_key=_env("VERIPHONE_API_KEY") or _env("VERIPHONE_KEY"),
        smartlead_api_key=_env("SMARTLEAD_API_KEY") or _env("SMARTLEAD_KEY"),
        smartlead_base_url=_env(
            "SMARTLEAD_FIND_EMAIL_BASE_URL",
            "https://prospect-api.smartlead.ai/api/v1/search-email-leads",
        ).rstrip("/"),
    )


settings = load_settings()
