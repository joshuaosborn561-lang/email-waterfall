from __future__ import annotations

from typing import Any, Callable

from email_waterfall import source as table_source
from email_waterfall.clients import ClientConfig
from email_waterfall.need import (
    CAP_EMAIL,
    CAP_PEOPLE,
    CAP_PHONE,
    allows,
    capabilities,
    using_need,
)
from email_waterfall.vendors.base import PersonHit, PhoneHit

from email_waterfall._wf.const import (
    TIER_RANK,
    classify_rows,
    _fill_row_domain,
    tier_allowed,
)
from email_waterfall._wf.engine import Waterfall, _int_attr


def _sync():
    from email_waterfall import waterfall as w
    return w.supabase_sync


ProgressCallback = Callable[[dict[str, Any]], None]


def _phone_type_of(wf: Waterfall, *numbers: str) -> str:
    for number in numbers:
        verdict = wf.cached_phone_verdict(number or "")
        if verdict is not None:
            return verdict.phone_type
    return ""


def _company_contact_rows(
    client: ClientConfig,
    item: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    row = item["row"]
    email = item["email"]
    email_tier = item["email_tier"]
    dm_tier = item["dm_tier"]
    person = item["person"]
    domain = row.get("domain") or ""
    if not domain:
        return None, None

    company = _sync().company_row(
        client_tag=client.tag,
        domain=domain,
        company_name=row.get("company_name") or "",
        source=row.get("source") or "waterfall",
        place=row.get("place_id") or "",
        address_city=row.get("city") or "",
        address_state=row.get("state") or "",
        email_source_tier=email_tier if email_tier != "input" else email_tier,
        dm_source_tier=dm_tier,
        source_tier={
            k: v for k, v in {"email": email_tier, "dm": dm_tier}.items() if v
        },
        dm_lookup_status="found" if dm_tier else "not_found",
    )

    first = row["first_name"]
    last = row["last_name"]
    title = row.get("title") or ""
    if person:
        first = person.first_name or first
        last = person.last_name or last
        title = person.title or title
    cellphone = item.get("phone") or ""
    line_type = item.get("phone_type") or ""
    contact: dict[str, Any] | None = None
    if first or last or email or cellphone or line_type:
        contact = _sync().contact_row(
            client_tag=client.tag,
            domain=domain,
            first_name=first,
            last_name=last,
            job_title=title,
            email=email,
            email_status="found" if email else "",
            linkedin_url=(person.linkedin_url if person else "")
            or row.get("linkedin_url")
            or "",
            cellphone=cellphone,
            line_type=line_type,
            contact_city=row.get("city") or "",
            contact_state=row.get("state") or "",
            source_tool=email_tier or dm_tier or item.get("phone_tier") or "waterfall",
            source_tier=email_tier or dm_tier or item.get("phone_tier") or "",
            place_id=row.get("place_id") or "",
            confidence=0.7 if email or dm_tier or cellphone else 0.0,
        )
    return company, contact


def _verify_only_row(wf: Waterfall, row: dict[str, Any]) -> dict[str, Any]:
    """Veriphone an existing number. No finder HTTP."""
    email = row.get("email") or ""
    existing = (row.get("phone") or "").strip()
    phone = existing
    phone_type = ""
    phone_tier = "input" if existing else ""
    with using_need("phone"):
        if existing and not wf.veriphone.enabled:
            wf._bump("veriphone", "skipped_unconfigured")
        elif existing:
            result = wf._phone_verdict(existing)
            if result is not None:
                phone_type = result.phone_type
                phone_tier = "veriphone"
                if result.is_mobile:
                    phone = result.e164 or result.phone
                    row["phone"] = phone
                    wf._bump("veriphone", "phone_hits")
                else:
                    wf._bump("veriphone", "rejected")
    return {
        "row": row,
        "email": email,
        "email_tier": "input" if email else "",
        "dm_tier": "",
        "person": None,
        "phone": phone,
        "phone_checked": existing,
        "phone_type": phone_type,
        "phone_tier": phone_tier,
    }


def _enrich_one_row(
    wf: Waterfall,
    row: dict[str, Any],
    *,
    need_norm: str,
    include_fullenrich: bool,
) -> dict[str, Any]:
    if wf.verify_only:
        return _verify_only_row(wf, row)
    with using_need(need_norm, getattr(wf, "caps", None)):
        email = row.get("email") or ""
        email_tier = "input" if email else ""
        person: PersonHit | None = None
        dm_tier = ""
        want_dm = allows(need_norm, CAP_PEOPLE)
        want_email = allows(need_norm, CAP_EMAIL)
        want_phone = allows(need_norm, CAP_PHONE)
        original_phone = (row.get("phone") or "").strip()

        if want_dm:
            person = wf.resolve_dm(row)
            if person:
                dm_tier = person.source_tier
                if not row["first_name"] and person.first_name:
                    row["first_name"] = person.first_name
                    row["last_name"] = person.last_name
                    row["full_name"] = person.name
                if not row.get("title") and person.title:
                    row["title"] = person.title
                if person.linkedin_url and not row.get("linkedin_url"):
                    row["linkedin_url"] = person.linkedin_url
                if want_phone and person.phone and not row.get("phone"):
                    row["phone"] = person.phone
                _fill_row_domain(row, email=person.email, raw=person.raw)
                if person.source_tier == "aiark":
                    pid = str((person.raw or {}).get("id") or "").strip()
                    if pid and not row.get("ai_ark_id"):
                        row["ai_ark_id"] = pid
                if person.email and not email:
                    email = person.email
                    email_tier = person.source_tier

        if want_email and not email:
            hit = wf.resolve_email(row, include_fullenrich=include_fullenrich)
            if hit:
                email, email_tier = hit.email, hit.source_tier
                extra_phone = getattr(hit, "phone", "") or ""
                if want_phone and extra_phone and not row.get("phone"):
                    row["phone"] = extra_phone
                _fill_row_domain(row, email=email, raw=hit.raw)

        phone_hit: PhoneHit | None = None
        if want_phone:
            phone_hit = wf.resolve_phone(row, email=email)
            if phone_hit:
                phone = phone_hit.phone
                row["phone"] = phone
            elif wf._phone_only():
                # Rejected by Veriphone: do not write the input landline.
                phone = ""
                row["phone"] = ""
            else:
                phone = row.get("phone") or ""
        else:
            wf.record_phone_skips(row, email=email)
            phone = row.get("phone") or ""

        phone_type = ""
        raw_vp = ((phone_hit.raw if phone_hit else {}) or {}).get("veriphone") or {}
        if isinstance(raw_vp, dict):
            phone_type = str(raw_vp.get("phone_type") or "")
        if not phone_type:
            phone_type = _phone_type_of(
                wf, phone, original_phone, row.get("phone") or ""
            )

        return {
            "row": row,
            "email": email,
            "email_tier": email_tier,
            "dm_tier": dm_tier,
            "person": person,
            "phone": phone,
            "phone_checked": original_phone or phone,
            "phone_type": phone_type,
            "phone_tier": phone_hit.source_tier if phone_hit else "",
        }


def _int_attr(obj: Any, name: str) -> int:
    val = getattr(obj, name, 0)
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _tier_warnings(
    tier_stats: dict[str, dict[str, Any]],
    *,
    need_norm: str = "both",
    veriphone_enabled: bool = True,
    verify_only: bool = False,
) -> list[str]:
    """Surface a tier that is failing, not merely missing. 0 hits + 2000 errors must not look quiet."""
    warnings: list[str] = []
    for name, stats in tier_stats.items():
        calls = int(stats.get("vendor_calls") or stats.get("calls") or 0)
        errors = int(stats.get("errors") or 0)
        if calls >= 10 and errors * 2 > calls:
            warnings.append(name)
        if stats.get("disabled") and name not in warnings:
            reason = stats.get("disabled_reason") or "disabled"
            warnings.append(f"{name}: {reason}")
    if (need_norm == "phone" or verify_only) and not veriphone_enabled:
        warnings.append("veriphone_unconfigured")
    return warnings


def _tier_breakdown(wf: Waterfall, max_tier_n: str) -> dict[str, dict[str, Any]]:
    for name, vendor in (
        ("getleads", wf.getleads),
        ("smartlead", wf.smartlead),
        ("aiark", wf.ai_ark),
        ("leadmagic", wf.leadmagic),
        ("prospeo", wf.prospeo),
        ("fullenrich", wf.fullenrich),
        ("veriphone", wf.veriphone),
    ):
        wf.tier_stats[name]["vendor_calls"] = _int_attr(vendor, "calls")
        wf.tier_stats[name]["vendor_hits"] = _int_attr(vendor, "hits")
        wf.tier_stats[name]["errors"] = _int_attr(vendor, "errors")
        first_err = getattr(vendor, "first_error", None)
        if first_err:
            wf.tier_stats[name]["first_error"] = str(first_err)[:300]
        charged = getattr(vendor, "credits_charged", None)
        if charged is not None:
            wf.tier_stats[name]["credits_charged"] = int(charged or 0)
        disabled_reason = getattr(wf, "_circuit_disabled", {}).get(name)
        if disabled_reason:
            wf.tier_stats[name]["disabled"] = True
            wf.tier_stats[name]["disabled_reason"] = disabled_reason
        snap = getattr(vendor, "credit_snapshot", None)
        if callable(snap):
            credits = snap()
            wf.tier_stats[name]["credits_available"] = credits.get("available")
            wf.tier_stats[name]["credits_total"] = credits.get("total")
            wf.tier_stats[name]["credits_used"] = credits.get("used")
            wf.tier_stats[name]["credits_exhausted"] = bool(credits.get("exhausted"))

    tier_breakdown: dict[str, dict[str, Any]] = {}
    for tier_name, stats in wf.tier_stats.items():
        allowed = tier_allowed(tier_name, max_tier_n) if tier_name in TIER_RANK else True
        row = {
            "attempts": int(stats.get("calls") or 0),
            "email_hits": int(stats.get("email_hits") or 0),
            "dm_hits": int(stats.get("dm_hits") or 0),
            "phone_hits": int(stats.get("phone_hits") or 0),
            "vendor_calls": int(stats.get("vendor_calls") or 0),
            "vendor_hits": int(stats.get("vendor_hits") or 0),
            "errors": int(stats.get("errors") or 0),
            "allowed_by_max_tier": allowed,
            "estimated_cost_usd": 0.0,
        }
        if "credits_available" in stats:
            row["credits_available"] = stats.get("credits_available")
            row["credits_total"] = stats.get("credits_total")
            row["credits_used"] = stats.get("credits_used")
            row["credits_exhausted"] = bool(stats.get("credits_exhausted"))
        if stats.get("first_error"):
            row["first_error"] = stats["first_error"]
        if "credits_charged" in stats:
            row["credits_charged"] = stats["credits_charged"]
        if stats.get("disabled"):
            row["disabled"] = True
            row["disabled_reason"] = stats.get("disabled_reason")
        tier_breakdown[tier_name] = row
    return tier_breakdown


def _result_payload(
    *,
    parsed_count: int,
    client: ClientConfig,
    need_norm: str,
    max_tier_n: str,
    titles: list[str],
    require_title_match: bool,
    wf: Waterfall,
    companies_upserted: int,
    contacts_written: int,
    emails_found: int,
    dms_found: int,
    phones_found: int,
    companies_done: int | None = None,
    companies_total: int | None = None,
) -> dict[str, Any]:
    tier_breakdown = _tier_breakdown(wf, max_tier_n)
    warnings = _tier_warnings(
        wf.tier_stats,
        need_norm=need_norm,
        veriphone_enabled=bool(getattr(wf.veriphone, "enabled", False)),
        verify_only=bool(getattr(wf, "verify_only", False)),
    )
    out: dict[str, Any] = {
        "rows_in": parsed_count,
        "companies_upserted": companies_upserted,
        "contacts_written": contacts_written,
        "emails_found": emails_found,
        "dms_found": dms_found,
        "phones_found": phones_found,
        "verify_only": bool(getattr(wf, "verify_only", False)),
        "phones_rejected_not_mobile": int(
            (wf.tier_stats.get("veriphone") or {}).get("rejected") or 0
        ),
        "modes": getattr(wf, "modes", None) or classify_rows([]),
        "tier_stats": dict(wf.tier_stats),
        "tier_breakdown": tier_breakdown,
        "warnings": warnings,
        "need": need_norm,
        "need_capabilities": sorted(getattr(wf, "caps", None) or capabilities(need_norm)),
        "suppressed_by_need": dict(getattr(wf, "suppressed_by_need", {}) or {}),
        "max_tier": max_tier_n,
        "client_tag": client.tag,
        "companies_table": client.companies_table,
        "contacts_table": client.contacts_table,
        "target_titles": titles,
        "require_title_match": bool(require_title_match),
        "vendors_enabled": {
            "getleads": wf.getleads.enabled and wf._allowed("getleads"),
            "smartlead": wf.smartlead.enabled and wf._allowed("smartlead"),
            "aiark": wf.ai_ark.enabled and wf._allowed("aiark"),
            "leadmagic": wf.leadmagic.enabled and wf._allowed("leadmagic"),
            "prospeo": wf.prospeo.enabled and wf._allowed("prospeo"),
            "fullenrich": wf.fullenrich.enabled and wf._allowed("fullenrich"),
            "veriphone": bool(wf.veriphone.enabled),
        },
    }
    if companies_done is not None:
        out["companies_done"] = companies_done
    if companies_total is not None:
        out["companies_total"] = companies_total
    return out


def _maybe_writeback(src: table_source.TableSource | None, item: dict[str, Any]) -> None:
    if src is None:
        return
    email = item.get("email") or ""
    phone = item.get("phone") or item.get("phone_checked") or ""
    phone_type = item.get("phone_type") or ""
    found = bool(email or item.get("phone"))
    table_source.writeback_result(
        src,
        key=item["row"].get("_source_key"),
        status="found" if found else "not_found",
        email=email,
        email_status="found" if email else "not_found",
        vendor=item.get("email_tier")
        or item.get("phone_tier")
        or item.get("dm_tier")
        or "",
        phone=phone,
        phone_type=phone_type,
    )


def _write_company_contact(
    client: ClientConfig,
    item: dict[str, Any],
    *,
    write_supabase: bool,
) -> tuple[int, int]:
    company, contact = _company_contact_rows(client, item)
    companies = 0
    contacts = 0
    if write_supabase and company:
        companies = _sync().upsert_companies(client, [company])
        if contact:
            contacts = _sync().upsert_contacts(client, [contact])
    return companies, contacts


