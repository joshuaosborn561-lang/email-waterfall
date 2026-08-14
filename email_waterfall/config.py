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
    getleads_api_key: str
    getleads_base_url: str
    getleads_find_email_path: str
    getleads_people_path: str
    ai_ark_api_key: str
    leadmagic_api_key: str
    prospeo_api_key: str
    fullenrich_api_key: str

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
        getleads_api_key=_env("GETLEADS_API_KEY"),
        getleads_base_url=_env("GETLEADS_BASE_URL", "https://app.getleads.io/api").rstrip("/"),
        getleads_find_email_path=_env("GETLEADS_FIND_EMAIL_PATH", "/find-email"),
        getleads_people_path=_env("GETLEADS_PEOPLE_PATH", "/people"),
        ai_ark_api_key=_env("AI_ARK_API_KEY") or _env("AIARK_API_KEY"),
        leadmagic_api_key=_env("LEADMAGIC_API_KEY") or _env("LEADMAGIC_KEY"),
        prospeo_api_key=_env("PROSPEO_API_KEY"),
        fullenrich_api_key=_env("FULLENRICH_API_KEY"),
    )


settings = load_settings()
