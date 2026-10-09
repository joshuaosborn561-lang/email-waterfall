from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from email_waterfall import source as table_source
from email_waterfall.clients import ClientConfig, parse_target_titles
from email_waterfall.concurrency import company_concurrency
from email_waterfall.need import CAP_EMAIL, CAP_PHONE, allows, resolve_need, using_need

from email_waterfall._wf.const import (
    DEFAULT_APPROVE_COST_USD,
    DEFAULT_MAX_TIER,
    Need,
    STATUS_REFUSED_OVER_CEILING,
    _fill_row_domain,
    _has_inline_rows,
    _norm_row,
    _parse_rows,
    classify_rows,
    estimate_waterfall,
    resolve_approve_cost_usd,
    resolve_max_tier,
    resolve_skip_tiers,
)
from email_waterfall._wf.engine import Waterfall
from email_waterfall._wf.row import (
    ProgressCallback,
    _enrich_one_row,
    _maybe_writeback,
    _result_payload,
    _write_company_contact,
)


def _enrich_waterfall_serial(
    parsed: list[dict[str, Any]],
    *,
    client: ClientConfig,
    need_norm: str,
    max_tier_n: str,
    titles: list[str],
    require_title_match: bool,
    write_supabase: bool,
    wf: Waterfall,
    progress_callback: ProgressCallback | None = None,
    table_src: table_source.TableSource | None = None,
) -> dict[str, Any]:
    pending_fe: list[tuple[int, dict[str, Any]]] = []
    enriched: list[dict[str, Any]] = []

    for idx, row in enumerate(parsed):
        item = _enrich_one_row(wf, row, need_norm=need_norm, include_fullenrich=False)
        if (
            not wf.verify_only
            and allows(need_norm, CAP_EMAIL)
            and not item["email"]
            and row["first_name"]
            and row["last_name"]
            and (row["domain"] or row.get("company_name"))
            and wf.fullenrich.enabled
            and wf._allowed("fullenrich")
        ):
            pending_fe.append((idx, row))
        enriched.append(item)

    if pending_fe and wf.fullenrich.enabled and wf._allowed("fullenrich"):
        hits: list = []
        with using_need(need_norm, getattr(wf, "caps", None)):
            reserved = []
            for idx, r in pending_fe:
                if wf._bump_attempt("fullenrich", r):
                    reserved.append((idx, r))
            pending_fe = reserved
            if pending_fe:
                fe_rows = [
                    {
                        "first_name": r["first_name"],
                        "last_name": r["last_name"],
                        "domain": r["domain"],
                        "company_name": r.get("company_name") or "",
                    }
                    for _, r in pending_fe
                ]
                hits = wf.fullenrich.find_email_bulk(fe_rows)
        for (idx, _row), hit in zip(pending_fe, hits):
            if not hit:
                continue
            wf._bump("fullenrich", "email_hits")
            enriched[idx]["email"] = hit.email
            enriched[idx]["email_tier"] = hit.source_tier
            _fill_row_domain(enriched[idx]["row"], email=hit.email, raw=hit.raw)
            extra_phone = getattr(hit, "phone", "") or ""
            if allows(need_norm, CAP_PHONE) and extra_phone and not enriched[idx].get("phone"):
                accepted = wf._accept_phone(
                    extra_phone,
                    source_tier="fullenrich",
                    raw=hit.raw,
                    require_verify=True,
                )
                if accepted:
                    enriched[idx]["phone"] = accepted.phone
                    enriched[idx]["phone_tier"] = accepted.source_tier
                    enriched[idx]["row"]["phone"] = accepted.phone
                    wf._bump("fullenrich", "phone_hits")
            if allows(need_norm, CAP_PHONE) and not enriched[idx].get("phone"):
                phone_hit = wf.resolve_phone(
                    enriched[idx]["row"], email=hit.email
                )
                if phone_hit:
                    enriched[idx]["phone"] = phone_hit.phone
                    enriched[idx]["phone_tier"] = phone_hit.source_tier
                    raw_vp = (phone_hit.raw or {}).get("veriphone") or {}
                    if isinstance(raw_vp, dict) and raw_vp.get("phone_type"):
                        enriched[idx]["phone_type"] = raw_vp["phone_type"]
                    if not enriched[idx]["row"].get("phone"):
                        enriched[idx]["row"]["phone"] = phone_hit.phone
            elif not allows(need_norm, CAP_PHONE):
                wf.record_phone_skips(
                    enriched[idx]["row"], email=hit.email
                )
    elif pending_fe:
        wf.tier_stats["fullenrich"]["blocked_by_max_tier"] = len(pending_fe)

    companies_upserted = 0
    contacts_written = 0
    emails_found = 0
    dms_found = 0
    phones_found = 0

    for n, item in enumerate(enriched, start=1):
        if item["email"]:
            emails_found += 1
        if item["dm_tier"]:
            dms_found += 1
        if item.get("phone"):
            phones_found += 1
        upserted, written = _write_company_contact(
            client, item, write_supabase=write_supabase
        )
        companies_upserted += upserted
        contacts_written += written
        _maybe_writeback(table_src, item)
        if progress_callback:
            progress_callback(
                _result_payload(
                    parsed_count=len(parsed),
                    client=client,
                    need_norm=need_norm,
                    max_tier_n=max_tier_n,
                    titles=titles,
                    require_title_match=require_title_match,
                    wf=wf,
                    companies_upserted=companies_upserted,
                    contacts_written=contacts_written,
                    emails_found=emails_found,
                    dms_found=dms_found,
                    phones_found=phones_found,
                    companies_done=n,
                    companies_total=len(parsed),
                )
            )

    return _result_payload(
        parsed_count=len(parsed),
        client=client,
        need_norm=need_norm,
        max_tier_n=max_tier_n,
        titles=titles,
        require_title_match=require_title_match,
        wf=wf,
        companies_upserted=companies_upserted,
        contacts_written=contacts_written,
        emails_found=emails_found,
        dms_found=dms_found,
        phones_found=phones_found,
        companies_done=len(parsed),
        companies_total=len(parsed),
    )


def _enrich_waterfall_parallel(
    parsed: list[dict[str, Any]],
    *,
    client: ClientConfig,
    need_norm: str,
    max_tier_n: str,
    titles: list[str],
    require_title_match: bool,
    write_supabase: bool,
    wf: Waterfall,
    progress_callback: ProgressCallback | None = None,
    table_src: table_source.TableSource | None = None,
) -> dict[str, Any]:
    total = len(parsed)
    progress_lock = threading.Lock()
    counters = {
        "companies_done": 0,
        "companies_upserted": 0,
        "contacts_written": 0,
        "emails_found": 0,
        "dms_found": 0,
        "phones_found": 0,
    }

    def _report() -> None:
        if not progress_callback:
            return
        with progress_lock:
            progress_callback(
                _result_payload(
                    parsed_count=total,
                    client=client,
                    need_norm=need_norm,
                    max_tier_n=max_tier_n,
                    titles=titles,
                    require_title_match=require_title_match,
                    wf=wf,
                    companies_upserted=counters["companies_upserted"],
                    contacts_written=counters["contacts_written"],
                    emails_found=counters["emails_found"],
                    dms_found=counters["dms_found"],
                    phones_found=counters["phones_found"],
                    companies_done=counters["companies_done"],
                    companies_total=total,
                )
            )

    def _process_row(row: dict[str, Any]) -> dict[str, Any]:
        row_copy = dict(row)
        item = _enrich_one_row(
            wf,
            row_copy,
            need_norm=need_norm,
            include_fullenrich=True,
        )
        upserted, written = _write_company_contact(
            client, item, write_supabase=write_supabase
        )
        _maybe_writeback(table_src, item)
        with progress_lock:
            counters["companies_done"] += 1
            counters["companies_upserted"] += upserted
            counters["contacts_written"] += written
            if item["email"]:
                counters["emails_found"] += 1
            if item["dm_tier"]:
                counters["dms_found"] += 1
            if item.get("phone"):
                counters["phones_found"] += 1
        _report()
        return item

    workers = min(company_concurrency(), total)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_process_row, row) for row in parsed]
        for fut in as_completed(futures):
            fut.result()

    return _result_payload(
        parsed_count=total,
        client=client,
        need_norm=need_norm,
        max_tier_n=max_tier_n,
        titles=titles,
        require_title_match=require_title_match,
        wf=wf,
        companies_upserted=counters["companies_upserted"],
        contacts_written=counters["contacts_written"],
        emails_found=counters["emails_found"],
        dms_found=counters["dms_found"],
        phones_found=counters["phones_found"],
        companies_done=total,
        companies_total=total,
    )


def enrich_waterfall(
    rows: Any = None,
    *,
    client_tag: str,
    need: Need = "both",
    max_tier: str = DEFAULT_MAX_TIER,
    target_titles: str | list[str] | None = "",
    require_title_match: bool = True,
    write_supabase: bool = True,
    parallel: bool = True,
    progress_callback: ProgressCallback | None = None,
    source: Any = None,
    source_table: Any = None,
    where: str | None = None,
    estimate_only: bool = False,
    writeback: bool | None = None,
    verify_only: bool = False,
    find_people: bool | None = None,
    find_email: bool | None = None,
    find_phone: bool | None = None,
    skip_tiers: Any = None,
    approve_cost_usd: float | int | str | None = None,
) -> dict[str, Any]:
    """Walk paid vendors per row; upsert isolated client tables; return counts.

    verify_only=True checks existing phone numbers with Veriphone and writes
    wf_phone / wf_phone_type (and contacts.line_type). Finder HTTP is skipped.
    find_people / find_email / find_phone override need when any is passed.
    approve_cost_usd is a hard USD ceiling (default $5). The job estimates
    first and refuses when the quote exceeds the ceiling; running spend that
    would exceed it stops with status=stopped_at_ceiling.
    """
    from email_waterfall import waterfall as host

    client = host.ensure_client(
        client_tag,
        write_supabase=bool(write_supabase) and not estimate_only,
    )
    max_tier_n, deprecated_max, max_notes = resolve_max_tier(max_tier)
    skip, deprecated_skip, skip_notes = resolve_skip_tiers(skip_tiers)
    control_warnings = max_notes + skip_notes
    ceiling = resolve_approve_cost_usd(
        approve_cost_usd, default=DEFAULT_APPROVE_COST_USD
    )
    need_norm, need_caps = resolve_need(
        need,
        find_people=find_people,
        find_email=find_email,
        find_phone=find_phone,
    )

    if isinstance(target_titles, list):
        titles = [str(t).strip() for t in target_titles if str(t).strip()]
        titles = titles or list(client.titles)
    else:
        titles = parse_target_titles(target_titles, client)

    merged = table_source.coerce_source(
        source, source_table=source_table, where=where, writeback=writeback
    )
    has_rows = _has_inline_rows(rows)
    has_source = merged is not None
    if has_rows and has_source:
        raise ValueError("pass rows or source_table/source, not both")
    if not has_rows and not has_source:
        if rows is None or (isinstance(rows, str) and not rows.strip()):
            raise ValueError("rows or source_table is required")

    table_src: table_source.TableSource | None = None
    if has_source:
        table_src = table_source.parse_source(merged)
        if writeback is not None:
            table_src.writeback = bool(writeback)
        if estimate_only:
            table_src.writeback = False
        raw_rows = table_source.fetch_source_rows(table_src)
    else:
        raw_rows = _parse_rows(rows)

    parsed = [_norm_row(r) for r in raw_rows]
    quote = estimate_waterfall(
        parsed,
        max_tier=max_tier_n,
        need=need_norm,
        client=client,
        verify_only=bool(verify_only),
        caps=need_caps,
        skip_tiers=skip,
        approve_cost_usd=ceiling,
        warnings=control_warnings,
        deprecated_max_tier=deprecated_max,
        deprecated_tiers=deprecated_skip,
    )
    if estimate_only:
        return quote
    estimated_usd = float(quote.get("estimated_cost_usd") or 0.0)
    if estimated_usd > ceiling:
        refused = dict(quote)
        refused["ok"] = False
        refused["estimate_only"] = False
        refused["status"] = STATUS_REFUSED_OVER_CEILING
        refused["approve_cost_usd"] = ceiling
        refused["estimated_cost_usd"] = estimated_usd
        refused["spend"] = 0
        refused["reason"] = (
            f"estimated_cost_usd={estimated_usd} exceeds "
            f"approve_cost_usd={ceiling}"
        )
        return refused
    if verify_only:
        parsed = [
            r
            for r in parsed
            if r.get("phone") or r.get("domain") or r.get("mode") == "name_company"
        ]
    else:
        parsed = [r for r in parsed if r.get("domain") or r.get("mode") == "name_company"]
    if not parsed:
        return {
            "rows_in": 0,
            "companies_upserted": 0,
            "contacts_written": 0,
            "emails_found": 0,
            "dms_found": 0,
            "phones_found": 0,
            "companies_done": 0,
            "companies_total": 0,
            "tier_stats": {},
            "need": need_norm,
            "need_capabilities": sorted(need_caps),
            "suppressed_by_need": {},
            "max_tier": max_tier_n,
            "skip_tiers": sorted(skip),
            "client_tag": client.tag,
            "companies_table": client.companies_table,
            "contacts_table": client.contacts_table,
            "target_titles": titles,
            "require_title_match": bool(require_title_match),
            "modes": classify_rows([]),
            "verify_only": bool(verify_only),
            "approve_cost_usd": ceiling,
            "spend": 0,
            "estimated_cost_usd": 0.0,
            "status": "completed",
            "warnings": control_warnings,
        }

    if table_src and table_src.writeback:
        table_source.ensure_writeback_columns(table_src)

    wf = Waterfall(
        max_tier=max_tier_n,
        target_titles=titles,
        fallback_titles=client.fallback_titles,
        require_title_match=bool(require_title_match),
        need=need_norm,
        verify_only=bool(verify_only),
        caps=need_caps,
        skip_tiers=skip,
        approve_cost_usd=ceiling,
    )
    wf.modes = classify_rows(parsed)
    wf.control_warnings = control_warnings
    wf.deprecated_max_tier = deprecated_max
    wf.deprecated_tiers = deprecated_skip
    wf.estimated_cost_usd = estimated_usd

    runner = _enrich_waterfall_parallel if parallel else _enrich_waterfall_serial
    return runner(
        parsed,
        client=client,
        need_norm=need_norm,
        max_tier_n=max_tier_n,
        titles=titles,
        require_title_match=bool(require_title_match),
        write_supabase=write_supabase,
        wf=wf,
        progress_callback=progress_callback,
        table_src=table_src,
    )

