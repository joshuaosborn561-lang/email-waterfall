"""`need` is an allowlist of vendor capabilities, not an output filter.

Applied before any vendor HTTP call. A future refactor that calls a phone
endpoint on need='email' / find_phone=false must raise rather than silently
spend credits.

Capabilities:
  email   — work-email finders (getleads, Smartlead, AI Ark export/single, …)
  phone   — mobile/cellphone finders (AI Ark mobile-phone-finder, Prospeo
            enrich_mobile, FullEnrich contact.phones) plus Veriphone
            /v2/verify so only phone_valid + phone_type=mobile is written
  people  — decision-maker / people search (not an email finder)

need='email'         → email only
need='phone'         → phone finders + people search (LinkedIn for the phone path)
                       + Veriphone mobile check; no email finders
need='dm'            → people search only
need='both'          → email + phone + people (phone is on)
need='people_email'  → people + email; phone off

Optional find_people / find_email / find_phone override need when any is
passed. Phone lookups stay off unless find_phone is true or need is
'both' / 'phone'.

verify_only (enrich_waterfall flag, not a need value) runs Veriphone on
numbers already on the row and writes wf_phone / line_type. Finder HTTP
is skipped.

PHONE_LOOKUPS_ENABLED kill switch (v1.7.0, default false)
---------------------------------------------------------
Phone lookups moved to the phone-waterfall MCP. While the switch is off the
`phone` capability is stripped from every resolved / bound capability set
(resolve_need, capabilities, set_need) and assert_capability(CAP_PHONE)
always raises, so no phone/mobile endpoint can be reached even by a code
path that forgets to check. need='both' runs as people_email; need='phone'
(phone requested without email) and verify_only short-circuit with
PHONE_DISABLED_WARNING and no paid work. PHONE_LOOKUPS_ENABLED=true restores
the old behavior exactly.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Iterable

CAP_EMAIL = "email"
CAP_PHONE = "phone"
CAP_PEOPLE = "people"

NEED_VALUES = ("email", "dm", "both", "phone", "people_email")

NEED_CAPABILITIES: dict[str, frozenset[str]] = {
    "email": frozenset({CAP_EMAIL}),
    "phone": frozenset({CAP_PHONE, CAP_PEOPLE}),
    "dm": frozenset({CAP_PEOPLE}),
    "both": frozenset({CAP_EMAIL, CAP_PHONE, CAP_PEOPLE}),
    "people_email": frozenset({CAP_PEOPLE, CAP_EMAIL}),
}

EMAIL_VENDORS = (
    "blitz",
    "getleads",
    "smartlead",
    "aiark",
    "pattern",
    "prospeo",
    "fullenrich",
)
PHONE_VENDORS = ("aiark", "prospeo", "fullenrich")
PHONE_ENDPOINTS = ("aiark", "prospeo", "fullenrich", "veriphone")
PEOPLE_VENDORS = ("getleads", "aiark")

# Actual billed credits on a hit. Miss is 0. Estimates use worst-case (assume hit).
# AI Ark: 0.5 people-search result, 1 email found, 5 mobile found.
# Prospeo: 1 email found, 10 mobile found.
AIARK_PEOPLE_CREDITS = 0.5
AIARK_EMAIL_CREDITS = 1.0
AIARK_MOBILE_CREDITS = 5.0
PROSPEO_EMAIL_CREDITS = 1.0
PROSPEO_MOBILE_CREDITS = 10.0

FUNCTION_CREDITS: dict[str, dict[str, float]] = {
    "aiark": {
        "people": AIARK_PEOPLE_CREDITS,
        "email": AIARK_EMAIL_CREDITS,
        "mobile": AIARK_MOBILE_CREDITS,
    },
    "prospeo": {
        "email": PROSPEO_EMAIL_CREDITS,
        "mobile": PROSPEO_MOBILE_CREDITS,
    },
}
PAY_ON_HIT = frozenset(FUNCTION_CREDITS)

# Backward-compatible names used by estimate rate notes / older tests.
AIARK_PHONE_CREDITS = AIARK_MOBILE_CREDITS
AIARK_BOTH_CREDITS = (
    AIARK_PEOPLE_CREDITS + AIARK_EMAIL_CREDITS + AIARK_MOBILE_CREDITS
)

_current_need: ContextVar[str] = ContextVar("enrich_need", default="both")
_current_caps: ContextVar[frozenset[str]] = ContextVar(
    "enrich_caps", default=NEED_CAPABILITIES["both"]
)


class NeedViolation(RuntimeError):
    """Vendor endpoint capability is not in the current `need` allowlist."""


PHONE_DISABLED_WARNING = "phone lookups disabled: use the phone-waterfall MCP"
PHONE_DISABLED_STATUS = "disabled"


def phone_lookups_enabled() -> bool:
    """PHONE_LOOKUPS_ENABLED kill switch. Read live so a settings reload applies."""
    from email_waterfall import config

    return bool(getattr(config.settings, "phone_lookups_enabled", False))


def _strip_phone(caps: frozenset[str]) -> frozenset[str]:
    if CAP_PHONE in caps and not phone_lookups_enabled():
        return caps - {CAP_PHONE}
    return caps


@dataclass(frozen=True)
class PhoneGate:
    """What the caller asked for vs what runs under the kill switch.

    disabled:   phone work was requested and dropped by the kill switch.
    phone_only: nothing but phone work was requested (need='phone',
                find_phone without find_email, or verify_only), so the call
                returns immediately with PHONE_DISABLED_WARNING.
    """

    requested_need: str
    requested_caps: frozenset[str]
    need: str
    caps: frozenset[str]
    disabled: bool
    phone_only: bool


def phone_gate(
    need: str | None = None,
    *,
    find_people: bool | None = None,
    find_email: bool | None = None,
    find_phone: bool | None = None,
    verify_only: bool = False,
) -> PhoneGate:
    if not _flags_passed(find_people, find_email, find_phone):
        req_need = normalize_need(need)
        req_caps = NEED_CAPABILITIES[req_need]
    else:
        req_caps = caps_from_flags(
            bool(find_people), bool(find_email), bool(find_phone)
        )
        req_need = label_for_caps(req_caps)
    if phone_lookups_enabled():
        return PhoneGate(req_need, req_caps, req_need, req_caps, False, False)
    wants_phone = CAP_PHONE in req_caps or bool(verify_only)
    caps = req_caps - {CAP_PHONE}
    label = label_for_caps(caps) if caps != req_caps else req_need
    phone_only = bool(verify_only) or (
        CAP_PHONE in req_caps and CAP_EMAIL not in req_caps
    )
    return PhoneGate(req_need, req_caps, label, caps, wants_phone, phone_only)


def phone_disabled_info() -> dict[str, object]:
    """`phone_lookups` block added to results while the kill switch is off."""
    return {
        "enabled": False,
        "status": PHONE_DISABLED_STATUS,
        "message": PHONE_DISABLED_WARNING,
    }


def normalize_need(need: str | None) -> str:
    need_norm = (need or "both").strip().lower()
    if need_norm not in NEED_VALUES:
        raise ValueError(
            "need must be 'email', 'dm', 'both', 'phone', or 'people_email'"
        )
    return need_norm


def _flags_passed(
    find_people: bool | None,
    find_email: bool | None,
    find_phone: bool | None,
) -> bool:
    return (
        find_people is not None
        or find_email is not None
        or find_phone is not None
    )


def caps_from_flags(
    find_people: bool,
    find_email: bool,
    find_phone: bool,
) -> frozenset[str]:
    caps: set[str] = set()
    if find_people:
        caps.add(CAP_PEOPLE)
    if find_email:
        caps.add(CAP_EMAIL)
    if find_phone:
        caps.add(CAP_PHONE)
    return frozenset(caps)


def label_for_caps(caps: frozenset[str]) -> str:
    for name, want in NEED_CAPABILITIES.items():
        if want == caps:
            return name
    parts = []
    if CAP_PEOPLE in caps:
        parts.append("people")
    if CAP_EMAIL in caps:
        parts.append("email")
    if CAP_PHONE in caps:
        parts.append("phone")
    return "_".join(parts) or "none"


def resolve_need(
    need: str | None = None,
    *,
    find_people: bool | None = None,
    find_email: bool | None = None,
    find_phone: bool | None = None,
) -> tuple[str, frozenset[str]]:
    """Return (need_label, capabilities). Flags override need when any is passed.

    With PHONE_LOOKUPS_ENABLED off the phone capability is dropped (and the
    label follows, e.g. both -> people_email).
    """
    gate = phone_gate(
        need, find_people=find_people, find_email=find_email, find_phone=find_phone
    )
    return gate.need, gate.caps


def capabilities(need: str) -> frozenset[str]:
    if need == _current_need.get():
        return _strip_phone(_current_caps.get())
    if need in NEED_CAPABILITIES:
        return _strip_phone(NEED_CAPABILITIES[need])
    return _strip_phone(NEED_CAPABILITIES[normalize_need(need)])


def current_capabilities() -> frozenset[str]:
    return _strip_phone(_current_caps.get())


def allows(need: str, capability: str) -> bool:
    return capability in capabilities(need)


def current_need() -> str:
    return _current_need.get()


def set_need(need: str, caps: frozenset[str] | None = None) -> object:
    if caps is None:
        need_norm = normalize_need(need)
        cap_val = NEED_CAPABILITIES[need_norm]
    else:
        need_norm = need
        cap_val = caps
    token_n = _current_need.set(need_norm)
    token_c = _current_caps.set(_strip_phone(cap_val))
    return (token_n, token_c)


def reset_need(token: object) -> None:
    token_n, token_c = token  # type: ignore[misc]
    _current_need.reset(token_n)
    _current_caps.reset(token_c)


@contextmanager
def using_need(need: str, caps: frozenset[str] | None = None) -> Iterator[str]:
    token = set_need(need, caps)
    try:
        yield _current_need.get()
    finally:
        reset_need(token)


def assert_capability(
    capability: str,
    *,
    vendor: str,
    endpoint: str,
) -> None:
    """Raise if this endpoint's capability is not allowed by the current need.

    Phone endpoints always raise while PHONE_LOOKUPS_ENABLED is off.
    """
    need = current_need()
    if capability == CAP_PHONE and not phone_lookups_enabled():
        raise NeedViolation(
            f"{PHONE_DISABLED_WARNING} ({vendor} {endpoint}); "
            "PHONE_LOOKUPS_ENABLED is false"
        )
    if capability in current_capabilities():
        return
    raise NeedViolation(
        f"need={need!r} does not allow {capability} "
        f"({vendor} {endpoint}). Do not call and discard."
    )


def function_credits(tier: str, function: str) -> float:
    """Published hit rate for one vendor function. 0 if unknown."""
    return float((FUNCTION_CREDITS.get(tier) or {}).get(function) or 0.0)


def function_worst_credits(tier: str, function: str) -> float:
    """Worst-case credits for one call (assume hit). AI Ark email includes search."""
    if tier == "aiark" and function == "email":
        return AIARK_PEOPLE_CREDITS + AIARK_EMAIL_CREDITS
    return function_credits(tier, function)


def credit_per_row(
    tier: str,
    need: str,
    default: float = 1.0,
    caps: frozenset[str] | None = None,
) -> float:
    """Conservative per-row credit quote (assume every function hits)."""
    rates = FUNCTION_CREDITS.get(tier)
    if not rates:
        return default
    use = caps if caps is not None else capabilities(need)
    total = 0.0
    if CAP_PEOPLE in use and "people" in rates:
        total += rates["people"]
    if CAP_EMAIL in use and "email" in rates:
        total += rates["email"]
        # Name+domain email still runs People Search when need has no people cap.
        if tier == "aiark" and CAP_PEOPLE not in use:
            total += rates["people"]
    if CAP_PHONE in use and "mobile" in rates:
        total += rates["mobile"]
    return total


def tier_serves_need(
    tier: str, need: str, caps: frozenset[str] | None = None
) -> bool:
    use = caps if caps is not None else capabilities(need)
    if CAP_EMAIL in use and tier in EMAIL_VENDORS:
        return True
    if CAP_PHONE in use and tier in PHONE_VENDORS:
        return True
    if CAP_PEOPLE in use and tier in PEOPLE_VENDORS:
        return True
    return False


def estimate_suppressed(
    vendor_rows: dict[str, int],
    need: str,
    caps: frozenset[str] | None = None,
) -> dict[str, int]:
    """Rows that would have hit a capability `need` forbids."""
    out: dict[str, int] = {}
    use = caps if caps is not None else capabilities(need)
    mapping: list[tuple[str, Iterable[str]]] = []
    if CAP_PHONE not in use:
        mapping.append(("phone", PHONE_ENDPOINTS))
    if CAP_EMAIL not in use:
        mapping.append(("email", EMAIL_VENDORS))
    if CAP_PEOPLE not in use:
        mapping.append(("people", PEOPLE_VENDORS))
    phone_n = max((int(vendor_rows.get(t) or 0) for t in PHONE_VENDORS), default=0)
    if not phone_n:
        phone_n = sum(int(vendor_rows.get(t) or 0) for t in vendor_rows)
    for capability, vendors in mapping:
        for tier in vendors:
            count = int(vendor_rows.get(tier) or 0)
            if capability == "phone":
                count = count or phone_n
            if count:
                out[f"{tier}_{capability}"] = count
    return out
