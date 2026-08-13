"""Person-name filters and ranked title matching."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .vendors.base import PersonHit

ENTITY_MARKERS = re.compile(
    r"\b(llc|inc\.?|corp\.?|ltd\.?|company|group|construction|builders?"
    r"|contractors?|services|partners?|development|corporation|honda|toyota"
    r"|dealership|motors)\b",
    re.I,
)
TITLE_ONLY = re.compile(
    r"^(project\s+manager|manager|president|ceo|owner|director|estimator|"
    r"superintendent|administrator|assistant|coordinator|engineer|"
    r"service\s+director|service\s+manager|general\s+manager|gm)$",
    re.I,
)

_SHORT = re.compile(r"^[a-z]{1,3}$")

# Extra aliases so ranked titles still hit common vendor wording.
_TITLE_ALIASES: dict[str, tuple[str, ...]] = {
    "service director": ("service director", "director of service"),
    "fixed operations director": (
        "fixed operations director",
        "fixed ops director",
        "director of fixed operations",
        "director of fixed ops",
        "fixed operations",
    ),
    "fixed ops director": (
        "fixed ops director",
        "fixed operations director",
        "director of fixed operations",
        "director of fixed ops",
    ),
    "service manager": ("service manager",),
    "warranty manager": ("warranty manager",),
    "warranty administrator": ("warranty administrator", "warranty admin"),
    "director of service": ("director of service", "service director"),
    "vp of service": (
        "vp of service",
        "vice president of service",
        "vp service",
        "service vp",
    ),
    "vice president of service": (
        "vice president of service",
        "vp of service",
        "vp service",
    ),
    "general manager": ("general manager", "gm"),
    "dealer principal": ("dealer principal", "dealer-principal"),
    "gm": ("gm", "general manager"),
    "vice president": ("vice president", "vp"),
    "vp": ("vp", "vice president"),
    "ceo": ("ceo", "chief executive"),
}


def looks_like_person(first: str, last: str = "") -> bool:
    """Reject titles, company names, and other non-human labels."""
    first = (first or "").strip()
    last = (last or "").strip()
    name = f"{first} {last}".strip()
    if len(name) < 3:
        return False
    if TITLE_ONLY.match(name):
        return False
    if ENTITY_MARKERS.search(name):
        return False
    if first.lower() in {"project", "general", "construction", "the", "our", "service"}:
        return False
    if not re.search(r"[A-Za-z]{2,}", first):
        return False
    last_alpha = re.sub(r"[^A-Za-z]", "", last)
    if last and len(last_alpha) < 2:
        return False
    return True


def _normalize_title(title: str) -> str:
    t = (title or "").lower()
    t = t.replace("&", " and ")
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = t.replace("vice president", "vp")
    t = t.replace("fixed operations", "fixed ops")
    return t


def _aliases(target: str) -> tuple[str, ...]:
    key = _normalize_title(target)
    extra = _TITLE_ALIASES.get(key, ())
    return (target, *extra)


def title_matches(title: str, target: str) -> bool:
    nt = _normalize_title(title)
    if not nt:
        return False
    for alias in _aliases(target):
        na = _normalize_title(alias)
        if not na:
            continue
        if _SHORT.match(na) or na in {"gm", "vp", "ceo"}:
            if re.search(rf"\b{re.escape(na)}\b", nt):
                return True
            continue
        if na in nt:
            return True
    return False


def title_rank(
    title: str,
    targets: list[str],
    fallback_titles: frozenset[str] | None = None,
) -> tuple[int, bool]:
    """Return (score, is_fallback). Higher score = earlier in the ranked list.

    Score 0 means no match.
    """
    fallback_titles = fallback_titles or frozenset()
    for i, target in enumerate(targets):
        if title_matches(title, target):
            is_fallback = _normalize_title(target) in fallback_titles
            return 100 - i, is_fallback
    return 0, False


@dataclass
class RankedPerson:
    person: PersonHit
    rank: int
    is_fallback: bool


def pick_best_person(
    people: list[PersonHit],
    *,
    targets: list[str],
    fallback_titles: frozenset[str] | None = None,
    require_title_match: bool = True,
) -> RankedPerson | None:
    scored: list[RankedPerson] = []
    for person in people:
        if not looks_like_person(person.first_name, person.last_name):
            continue
        rank, is_fallback = title_rank(
            person.title, targets, fallback_titles=fallback_titles
        )
        if require_title_match and rank <= 0:
            continue
        scored.append(RankedPerson(person=person, rank=rank, is_fallback=is_fallback))
    if not scored:
        return None
    scored.sort(key=lambda r: (-r.rank, r.is_fallback))
    return scored[0]
