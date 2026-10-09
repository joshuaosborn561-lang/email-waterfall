from __future__ import annotations

from typing import Any

from email_waterfall.need import resolve_need, using_need

from email_waterfall._wf.const import (
    DEFAULT_APPROVE_COST_USD_ONE,
    STATUS_REFUSED_OVER_CEILING,
    _norm_row,
    estimate_waterfall,
    resolve_approve_cost_usd,
    resolve_max_tier,
    resolve_skip_tiers,
)
from email_waterfall._wf.engine import Waterfall
from email_waterfall._wf.row import _enrich_one_row, _write_company_contact


def compact_person_hit(item: dict[str, Any]) -> dict[str, Any]:
    """One-person lookup payload for ReplyHandler / enrich_person. Not a bulk dump."""
    row = item.get("row") or {}
    person = item.get("person")
    linkedin = ""
    if person and getattr(person, "linkedin_url", ""):
        linkedin = person.linkedin_url
    linkedin = linkedin or row.get("linkedin_url") or ""
    domain = row.get("domain") or ""
    return {
        "email": item.get("email") or "",
        "phone": item.get("phone") or "",
        "linkedin_url": linkedin,
        "website": f"https://{domain}" if domain else "",
        "domain": domain,
        "first_name": row.get("first_name") or "",
        "last_name": row.get("last_name") or "",
        "email_tier": item.get("email_tier") or "",
        "phone_tier": item.get("phone_tier") or "",
        "dm_tier": item.get("dm_tier") or "",
        "phone_type": item.get("phone_type") or "",
    }


def enrich_one_person(
    *,
    client_tag: str,
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    full_name: str = "",
    linkedin_url: str = "",
    company_name: str = "",
    domain: str = "",
    need: str = "both",
    max_tier: str = "fullenrich",
    write_supabase: bool = False,
    approve_cost_usd: float | int | str | None = None,
    skip_tiers: Any = None,
) -> dict[str, Any]:
    """Run the waterfall for one prospect and return that compact hit.

    ReplyHandler calls this via POST /enrich-one so Slack cards get phone +
    LinkedIn without ReplyHandler owning vendor HTTP. Bulk enrich_waterfall
    still returns counts only.
    """
    if not (client_tag or "").strip():
        raise ValueError("client_tag is required")
    from email_waterfall import waterfall as host

    client = host.ensure_client(client_tag, write_supabase=bool(write_supabase))
    max_tier_n, deprecated_max, max_notes = resolve_max_tier(max_tier)
    skip, deprecated_skip, skip_notes = resolve_skip_tiers(skip_tiers)
    ceiling = resolve_approve_cost_usd(
        approve_cost_usd, default=DEFAULT_APPROVE_COST_USD_ONE
    )
    need_norm, need_caps = resolve_need(need)
    raw = {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "name": full_name,
        "linkedin_url": linkedin_url,
        "company_name": company_name,
        "domain": domain,
    }
    row = _norm_row(raw)
    if not row.get("domain") and row.get("mode") != "name_company":
        return {
            "ok": False,
            "reason": "need_domain_or_name_company",
            "email": (email or "").strip().lower(),
            "phone": "",
            "linkedin_url": (linkedin_url or "").strip(),
            "website": "",
            "email_tier": "input" if email else "",
            "phone_tier": "",
            "max_tier": max_tier_n,
            "need": need_norm,
            "client_tag": client.tag,
            "approve_cost_usd": ceiling,
        }
    quote = estimate_waterfall(
        [row],
        max_tier=max_tier_n,
        need=need_norm,
        client=client,
        caps=need_caps,
        skip_tiers=skip,
        approve_cost_usd=ceiling,
        warnings=max_notes + skip_notes,
        deprecated_max_tier=deprecated_max,
        deprecated_tiers=deprecated_skip,
    )
    estimated_usd = float(quote.get("estimated_cost_usd") or 0.0)
    if estimated_usd > ceiling:
        return {
            "ok": False,
            "reason": (
                f"estimated_cost_usd={estimated_usd} exceeds "
                f"approve_cost_usd={ceiling}"
            ),
            "status": STATUS_REFUSED_OVER_CEILING,
            "estimated_cost_usd": estimated_usd,
            "approve_cost_usd": ceiling,
            "max_tier": max_tier_n,
            "need": need_norm,
            "client_tag": client.tag,
            "warnings": max_notes + skip_notes,
            "email": (email or "").strip().lower(),
            "phone": "",
            "linkedin_url": (linkedin_url or "").strip(),
            "website": "",
        }
    wf = Waterfall(
        max_tier=max_tier_n,
        target_titles=list(client.titles),
        fallback_titles=client.fallback_titles,
        require_title_match=False,
        need=need_norm,
        caps=need_caps,
        skip_tiers=skip,
        approve_cost_usd=ceiling,
    )
    wf.control_warnings = max_notes + skip_notes
    wf.deprecated_max_tier = deprecated_max
    wf.deprecated_tiers = deprecated_skip
    wf.estimated_cost_usd = estimated_usd
    with using_need(need_norm, need_caps):
        item = _enrich_one_row(
            wf, row, need_norm=need_norm, include_fullenrich=True
        )
    if write_supabase:
        _write_company_contact(client, item, write_supabase=True)
    hit = compact_person_hit(item)
    hit.update(
        {
            "ok": not wf.stopped_at_ceiling,
            "max_tier": max_tier_n,
            "need": need_norm,
            "client_tag": client.tag,
            "approve_cost_usd": ceiling,
            "estimated_cost_usd": estimated_usd,
            "spend": wf.spend_usd,
            "status": (
                "stopped_at_ceiling" if wf.stopped_at_ceiling else "completed"
            ),
            "warnings": max_notes + skip_notes,
            "sources": {
                "email": hit.get("email_tier") or None,
                "phone": hit.get("phone_tier") or None,
                "linkedin": hit.get("dm_tier") or None,
            },
        }
    )
    if deprecated_max:
        hit["deprecated_max_tier"] = deprecated_max
    if deprecated_skip:
        hit["deprecated_tiers"] = deprecated_skip
    if wf.stopped_at_ceiling:
        hit["reason"] = "stopped_at_ceiling"
    return hit
