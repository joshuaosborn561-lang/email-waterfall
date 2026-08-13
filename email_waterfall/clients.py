"""Per-client ICP: isolated tables and ranked DM titles.

client_tag is required. Never write to a shared contacts table.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CLIENT_TAGS = ("basco", "peterson")

# Carlos — franchise new-car dealership rooftops near Clifton, NJ.
BASCO_TITLES: tuple[str, ...] = (
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

# Kyle — commercial roofing / GCs / property managers (Dallas-Fort Worth).
PETERSON_TITLES: tuple[str, ...] = (
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

# Basco: GM / Dealer Principal only if no higher-ranked service title is found.
BASCO_FALLBACK_TITLES: frozenset[str] = frozenset(
    {
        "general manager",
        "dealer principal",
        "gm",
    }
)

ALIASES: dict[str, tuple[str, ...]] = {
    "basco": ("basco", "carlos", "vasco"),
    "peterson": ("peterson", "kyle"),
}


@dataclass(frozen=True)
class ClientConfig:
    tag: str
    companies_table: str
    contacts_table: str
    titles: tuple[str, ...]
    fallback_titles: frozenset[str] = field(default_factory=frozenset)
    owner: str = ""
    icp: str = ""


CLIENTS: dict[str, ClientConfig] = {
    "basco": ClientConfig(
        tag="basco",
        companies_table="basco_companies",
        contacts_table="basco_contacts",
        titles=BASCO_TITLES,
        fallback_titles=BASCO_FALLBACK_TITLES,
        owner="Carlos",
        icp="Franchise new-car dealership rooftops near Clifton, NJ. Target service / fixed-ops DMs.",
    ),
    "peterson": ClientConfig(
        tag="peterson",
        companies_table="peterson_companies",
        contacts_table="peterson_contacts",
        titles=PETERSON_TITLES,
        fallback_titles=frozenset(),
        owner="Kyle",
        icp="Commercial roofing, GCs, and property managers in Dallas-Fort Worth.",
    ),
}


def normalize_client_tag(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        raise ValueError("client_tag is required (basco | peterson)")
    for tag, aliases in ALIASES.items():
        if raw in aliases:
            return tag
    raise ValueError(
        f"client_tag must be 'basco' or 'peterson'; got {value!r}. "
        "Never write to a shared contacts table."
    )


def get_client(client_tag: str | None) -> ClientConfig:
    return CLIENTS[normalize_client_tag(client_tag)]


def parse_target_titles(raw: str | None, client: ClientConfig) -> list[str]:
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    return parts or list(client.titles)
