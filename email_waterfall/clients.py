"""Per-client ICP: isolated tables and ranked DM titles.

Any snake_case client_tag works. Built-in: basco / peterson (legacy table names).
New tags write public.{tag}_wf_companies / public.{tag}_wf_contacts.
ensure_client (and enrich_waterfall auto-ensure) creates tables via ew_ensure_client.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any

# Carlos — franchise new-car dealership rooftops near Clifton, NJ.
SERVICE_TITLES: tuple[str, ...] = (
    "Service Director",
    "Fixed Operations Director",
    "Fixed Ops Director",
    "Service Manager",
    "Warranty Manager",
    "Warranty Administrator",
    "Director of Service",
    "VP of Service",
    "Vice President of Service",
    "General Manager",
    "Dealer Principal",
    "GM",
)
BASCO_TITLES = SERVICE_TITLES

# Kyle — commercial roofing / GCs / property managers (Dallas-Fort Worth).
OWNER_TITLES: tuple[str, ...] = (
    "Owner",
    "Founder",
    "Principal",
    "President",
    "Partner",
    "CEO",
    "Vice President",
    "VP",
    "Director",
    "General Manager",
)
PETERSON_TITLES = OWNER_TITLES

GOLIATH_TITLES: tuple[str, ...] = (
    "IT Director",
    "Director of IT",
    "Director of Information Technology",
    "Director of Technology",
    "VP of IT",
    "VP of Information Technology",
    "Vice President of Information Technology",
    "CIO",
    "Chief Information Officer",
    "IT Manager",
    "Head of IT",
    "Head of Information Technology",
    "CISO",
    "Chief Information Security Officer",
)

# Basco: GM / Dealer Principal only if no higher-ranked service title is found.
BASCO_FALLBACK_TITLES: frozenset[str] = frozenset(
    {
        "general manager",
        "dealer principal",
        "gm",
    }
)

RESERVED_TAGS = frozenset(
    {
        "lp",
        "public",
        "gc",
        "storage",
        "auth",
        "shared",
        "common",
        "default",
        "all",
    }
)

ALIASES: dict[str, tuple[str, ...]] = {
    "basco": ("basco", "carlos", "vasco"),
    "peterson": ("peterson", "kyle"),
}

LEGACY_TABLE_TAGS = frozenset({"basco", "peterson"})
_TAG_RE = re.compile(r"^[a-z][a-z0-9_]{0,46}$")


@dataclass
class ClientConfig:
    tag: str
    companies_table: str
    contacts_table: str
    titles: tuple[str, ...]
    fallback_titles: frozenset[str] = field(default_factory=frozenset)
    owner: str = ""
    icp: str = ""
    profile: str = "owner"
    display_name: str = ""


def _legacy_tables(tag: str) -> tuple[str, str]:
    if tag in LEGACY_TABLE_TAGS:
        return f"{tag}_companies", f"{tag}_contacts"
    return f"{tag}_wf_companies", f"{tag}_wf_contacts"


def _titles_for_profile(profile: str) -> tuple[str, ...]:
    return SERVICE_TITLES if profile == "service" else OWNER_TITLES


def _fallback_for(tag: str, profile: str) -> frozenset[str]:
    if tag == "basco" or profile == "service":
        return BASCO_FALLBACK_TITLES
    return frozenset()


def _builtin(tag: str) -> ClientConfig:
    companies, contacts = _legacy_tables(tag)
    if tag == "basco":
        return ClientConfig(
            tag="basco",
            companies_table=companies,
            contacts_table=contacts,
            titles=SERVICE_TITLES,
            fallback_titles=BASCO_FALLBACK_TITLES,
            owner="Carlos",
            display_name="Carlos",
            icp="Franchise new-car dealership rooftops near Clifton, NJ. Target service / fixed-ops DMs.",
            profile="service",
        )
    if tag == "peterson":
        return ClientConfig(
            tag="peterson",
            companies_table=companies,
            contacts_table=contacts,
            titles=OWNER_TITLES,
            fallback_titles=frozenset(),
            owner="Kyle",
            display_name="Kyle",
            icp="Commercial roofing, GCs, and property managers in Dallas-Fort Worth.",
            profile="owner",
        )
    if tag == "goliath":
        return ClientConfig(
            tag="goliath",
            companies_table=companies,
            contacts_table=contacts,
            titles=GOLIATH_TITLES,
            owner="goliath",
            display_name="goliath",
            icp="Client 'goliath' — ranked DM + email waterfall.",
            profile="owner",
        )
    if tag == "salesglider":
        return ClientConfig(
            tag="salesglider",
            companies_table=companies,
            contacts_table=contacts,
            titles=OWNER_TITLES,
            owner="salesglider",
            display_name="salesglider",
            icp="Client 'salesglider' — ranked DM + email waterfall.",
            profile="owner",
        )
    raise KeyError(tag)


_SEED_TAGS = ("basco", "peterson", "goliath", "salesglider")

CLIENTS: dict[str, ClientConfig] = {tag: _builtin(tag) for tag in _SEED_TAGS}

_lock = threading.Lock()
_registry: dict[str, ClientConfig] = dict(CLIENTS)
_db_loaded = False


def normalize_client_tag(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        raise ValueError("client_tag is required")
    for tag, aliases in ALIASES.items():
        if raw in aliases:
            return tag
    safe = re.sub(r"[^a-z0-9_]", "", raw)
    if safe in RESERVED_TAGS or safe.startswith("pg_"):
        raise ValueError(
            f"client_tag {value!r} is reserved. Never write to a shared contacts table."
        )
    if not _TAG_RE.match(safe):
        raise ValueError(
            f"client_tag must be snake_case (e.g. goliath, salesglider); got {value!r}"
        )
    return safe


def _from_row(row: dict[str, Any]) -> ClientConfig:
    tag = str(row.get("client_tag") or "").strip().lower()
    titles_raw = row.get("titles") or []
    if isinstance(titles_raw, str):
        titles = tuple(p.strip() for p in titles_raw.split(",") if p.strip())
    else:
        titles = tuple(str(t) for t in titles_raw if str(t).strip())
    profile = str(row.get("profile") or "owner").strip().lower()
    if profile not in ("owner", "service"):
        profile = "owner"
    if not titles:
        titles = _titles_for_profile(profile)
    companies = str(row.get("companies_table") or "").strip()
    contacts = str(row.get("contacts_table") or "").strip()
    if not companies or not contacts:
        companies, contacts = _legacy_tables(tag)
    display = str(row.get("display_name") or row.get("owner") or tag).strip()
    return ClientConfig(
        tag=tag,
        companies_table=companies,
        contacts_table=contacts,
        titles=titles,
        fallback_titles=_fallback_for(tag, profile),
        owner=display,
        display_name=display,
        icp=str(row.get("icp") or "").strip(),
        profile=profile,
    )


def _refresh_from_db() -> None:
    global _db_loaded
    try:
        from . import supabase_sync

        rows = supabase_sync.rpc("ew_list_clients", {})
    except Exception:
        _db_loaded = True
        return
    if rows is None:
        _db_loaded = True
        return
    if isinstance(rows, dict):
        rows = rows.get("clients") or rows.get("data") or []
    if not isinstance(rows, list):
        _db_loaded = True
        return
    with _lock:
        for row in rows:
            if not isinstance(row, dict) or not row.get("client_tag"):
                continue
            cfg = _from_row(row)
            _registry[cfg.tag] = cfg
            CLIENTS[cfg.tag] = cfg
        _db_loaded = True


def _ensure_db_loaded() -> None:
    if not _db_loaded:
        _refresh_from_db()


def _local_config(
    tag: str,
    *,
    profile: str = "owner",
    titles: tuple[str, ...] | None = None,
    display_name: str = "",
    icp: str = "",
) -> ClientConfig:
    profile_n = profile if profile in ("owner", "service") else "owner"
    if titles:
        title_tuple = titles
    elif tag in CLIENTS:
        title_tuple = CLIENTS[tag].titles
    else:
        title_tuple = _titles_for_profile(profile_n)
    companies, contacts = _legacy_tables(tag)
    display = display_name or tag
    return ClientConfig(
        tag=tag,
        companies_table=companies,
        contacts_table=contacts,
        titles=title_tuple,
        fallback_titles=_fallback_for(tag, profile_n),
        owner=display,
        display_name=display,
        icp=icp,
        profile=profile_n,
    )


def ensure_client(
    client_tag: str,
    *,
    display_name: str = "",
    profile: str = "owner",
    icp: str = "",
    target_titles: str | list[str] | None = "",
    write_supabase: bool = True,
) -> ClientConfig:
    """Register a client and create isolated write tables. Idempotent."""
    tag = normalize_client_tag(client_tag)
    profile_n = (profile or "owner").strip().lower()
    if profile_n not in ("owner", "service"):
        profile_n = "owner"
    if isinstance(target_titles, list):
        titles = tuple(str(t).strip() for t in target_titles if str(t).strip())
    else:
        titles = tuple(p.strip() for p in (target_titles or "").split(",") if p.strip())

    cfg: ClientConfig | None = None
    if write_supabase:
        try:
            from . import supabase_sync

            payload: dict[str, Any] = {
                "p_client_tag": tag,
                "p_display_name": display_name or tag,
                "p_profile": profile_n,
                "p_icp": icp or None,
            }
            if titles:
                payload["p_titles"] = list(titles)
            row = supabase_sync.rpc("ew_ensure_client", payload)
            if isinstance(row, dict) and row.get("client_tag"):
                cfg = _from_row(
                    {
                        **row,
                        "icp": icp or row.get("icp") or "",
                        "display_name": display_name or row.get("display_name") or tag,
                    }
                )
        except Exception:
            cfg = None

    if cfg is None:
        cfg = _local_config(
            tag,
            profile=profile_n,
            titles=titles or None,
            display_name=display_name,
            icp=icp,
        )

    with _lock:
        _registry[cfg.tag] = cfg
        CLIENTS[cfg.tag] = cfg
    return cfg


def get_client(client_tag: str | None) -> ClientConfig:
    tag = normalize_client_tag(client_tag)
    _ensure_db_loaded()
    with _lock:
        cached = _registry.get(tag)
    if cached:
        return cached
    return ensure_client(tag)


def list_registered_clients() -> list[ClientConfig]:
    _ensure_db_loaded()
    with _lock:
        return [cfg for _, cfg in sorted(_registry.items())]


def parse_target_titles(raw: str | None, client: ClientConfig) -> list[str]:
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    return parts or list(client.titles)
