"""`need` is an allowlist of vendor capabilities, not an output filter.

Applied before any vendor HTTP call. A future refactor that calls a phone
endpoint on need='email' must raise rather than silently spend credits.

Capabilities:
  email   — work-email finders (getleads, Smartlead, AI Ark export/single, …)
  phone   — mobile/cellphone finders (AI Ark mobile-phone-finder, LeadMagic
            mobile-finder, Prospeo enrich_mobile)
  people  — decision-maker / people search (not an email finder)

need='email'  → email only
need='phone'  → phone finders + people search (LinkedIn for the phone path);
                no email finders
need='dm'     → people search only
need='both'   → email + phone + people
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Iterable

CAP_EMAIL = "email"
CAP_PHONE = "phone"
CAP_PEOPLE = "people"

NEED_VALUES = ("email", "dm", "both", "phone")

NEED_CAPABILITIES: dict[str, frozenset[str]] = {
    "email": frozenset({CAP_EMAIL}),
    "phone": frozenset({CAP_PHONE, CAP_PEOPLE}),
    "dm": frozenset({CAP_PEOPLE}),
    "both": frozenset({CAP_EMAIL, CAP_PHONE, CAP_PEOPLE}),
}

EMAIL_VENDORS = (
    "getleads",
    "smartlead",
    "aiark",
    "leadmagic",
    "prospeo",
    "fullenrich",
)
PHONE_VENDORS = ("aiark", "leadmagic", "prospeo")
PEOPLE_VENDORS = ("getleads", "aiark", "leadmagic")

# AI Ark 1.5 is the email+phone bundle used in estimate_only. Email-only is 1.0;
# phone-only (mobile-phone-finder, no export/single) is 0.5.
AIARK_EMAIL_CREDITS = 1.0
AIARK_PHONE_CREDITS = 0.5
AIARK_BOTH_CREDITS = 1.5

_current_need: ContextVar[str] = ContextVar("enrich_need", default="both")


class NeedViolation(RuntimeError):
    """Vendor endpoint capability is not in the current `need` allowlist."""


def normalize_need(need: str | None) -> str:
    need_norm = (need or "both").strip().lower()
    if need_norm not in NEED_VALUES:
        raise ValueError("need must be 'email', 'dm', 'both', or 'phone'")
    return need_norm


def capabilities(need: str) -> frozenset[str]:
    return NEED_CAPABILITIES[normalize_need(need)]


def allows(need: str, capability: str) -> bool:
    return capability in capabilities(need)


def current_need() -> str:
    return _current_need.get()


def set_need(need: str) -> object:
    return _current_need.set(normalize_need(need))


def reset_need(token: object) -> None:
    _current_need.reset(token)  # type: ignore[arg-type]


@contextmanager
def using_need(need: str) -> Iterator[str]:
    need_norm = normalize_need(need)
    token = set_need(need_norm)
    try:
        yield need_norm
    finally:
        reset_need(token)


def assert_capability(
    capability: str,
    *,
    vendor: str,
    endpoint: str,
) -> None:
    """Raise if this endpoint's capability is not allowed by the current need."""
    need = current_need()
    if allows(need, capability):
        return
    raise NeedViolation(
        f"need={need!r} does not allow {capability} "
        f"({vendor} {endpoint}). Do not call and discard."
    )


def credit_per_row(tier: str, need: str, default: float = 1.0) -> float:
    """Per-row credit quote. AI Ark 1.5 is email+phone; need gates that."""
    if tier != "aiark":
        return default
    caps = capabilities(need)
    want_email = CAP_EMAIL in caps
    want_phone = CAP_PHONE in caps
    if want_email and want_phone:
        return AIARK_BOTH_CREDITS
    if want_phone and not want_email:
        return AIARK_PHONE_CREDITS
    return AIARK_EMAIL_CREDITS


def tier_serves_need(tier: str, need: str) -> bool:
    caps = capabilities(need)
    if CAP_EMAIL in caps and tier in EMAIL_VENDORS:
        return True
    if CAP_PHONE in caps and tier in PHONE_VENDORS:
        return True
    if CAP_PEOPLE in caps and tier in PEOPLE_VENDORS:
        return True
    return False


def estimate_suppressed(vendor_rows: dict[str, int], need: str) -> dict[str, int]:
    """Rows that would have hit a capability `need` forbids."""
    out: dict[str, int] = {}
    caps = capabilities(need)
    mapping: list[tuple[str, Iterable[str]]] = []
    if CAP_PHONE not in caps:
        mapping.append(("phone", PHONE_VENDORS))
    if CAP_EMAIL not in caps:
        mapping.append(("email", EMAIL_VENDORS))
    if CAP_PEOPLE not in caps:
        mapping.append(("people", PEOPLE_VENDORS))
    for capability, vendors in mapping:
        for tier in vendors:
            count = int(vendor_rows.get(tier) or 0)
            if count:
                out[f"{tier}_{capability}"] = count
    return out
