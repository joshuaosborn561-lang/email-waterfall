"""Write enrichment results to isolated public.{client}_companies / _contacts.

Hard rules:
- client_tag required; never a shared contacts table
- companies upsert on domain, after deduping the batch by domain
- contacts with email → ignore-duplicates on (domain, email)
- null-email contacts insert separately (no ON CONFLICT)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib import error, request

from .clients import ClientConfig
from .config import load_settings

BATCH_SIZE = 200


def supabase_config() -> dict[str, str]:
    cfg = load_settings()
    url = cfg.supabase_url.rstrip("/")
    key = cfg.supabase_key
    if not url or not key:
        raise RuntimeError(
            "Supabase not configured. Set SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_ANON_KEY)."
        )
    return {"url": url, "key": key}


def _headers(key: str, *, prefer: str) -> dict[str, str]:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": prefer,
        "Accept": "application/json",
    }


def _request(
    method: str,
    path: str,
    *,
    body: Any = None,
    prefer: str = "return=minimal",
) -> tuple[int, str]:
    cfg = supabase_config()
    url = f"{cfg['url']}/rest/v1/{path}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        headers=_headers(cfg["key"], prefer=prefer),
        method=method,
    )
    try:
        with request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Supabase {method} {url} failed ({exc.code}): {detail[:500]}"
        ) from exc


def _chunks(rows: list[dict[str, Any]], size: int = BATCH_SIZE):
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def merge_company(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Keep non-empty fields; prefer 'found' lookup status and later tiers."""
    out = dict(a)
    for key, val in b.items():
        if val in (None, "", {}, []):
            continue
        if key == "source_tier" and isinstance(val, dict):
            prev = out.get("source_tier") if isinstance(out.get("source_tier"), dict) else {}
            out["source_tier"] = {**prev, **val}
            continue
        if key == "dm_lookup_status" and out.get("dm_lookup_status") == "found":
            continue
        out[key] = val
    return out


def dedupe_companies(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per domain. Same-domain duplicates in one upsert raise Postgres 21000."""
    by_domain: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        domain = str(row.get("domain") or "").strip().lower()
        if not domain:
            continue
        row = {**row, "domain": domain}
        if domain not in by_domain:
            order.append(domain)
            by_domain[domain] = row
        else:
            by_domain[domain] = merge_company(by_domain[domain], row)
    return [by_domain[d] for d in order]


def dedupe_contacts_with_email(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        email = (row.get("email") or "").strip().lower()
        domain = (row.get("domain") or "").strip().lower()
        if not email or not domain:
            continue
        key = (domain, email)
        row = {**row, "domain": domain, "email": email}
        if key not in seen:
            order.append(key)
            seen[key] = row
        else:
            prev = seen[key]
            merged = dict(prev)
            for k, v in row.items():
                if v not in (None, "", [], {}):
                    merged[k] = v
            seen[key] = merged
    return [seen[k] for k in order]


def upsert_companies(client: ClientConfig, rows: list[dict[str, Any]]) -> int:
    rows = dedupe_companies(rows)
    if not rows:
        return 0
    written = 0
    for batch in _chunks(rows):
        _request(
            "POST",
            f"{client.companies_table}?on_conflict=domain",
            body=batch,
            prefer="resolution=merge-duplicates,return=minimal",
        )
        written += len(batch)
    return written


def insert_contacts(client: ClientConfig, rows: list[dict[str, Any]]) -> int:
    """Insert null-email rows. Do not use ON CONFLICT — email is null."""
    clean = [r for r in rows if not (r.get("email") or "").strip()]
    if not clean:
        return 0
    written = 0
    for batch in _chunks(clean):
        _request(
            "POST",
            client.contacts_table,
            body=batch,
            prefer="return=minimal",
        )
        written += len(batch)
    return written


def insert_contacts_ignore_conflict(
    client: ClientConfig, rows: list[dict[str, Any]]
) -> int:
    """Ignore duplicates on UNIQUE (domain, email). Email must be non-null."""
    clean = dedupe_contacts_with_email(rows)
    if not clean:
        return 0
    written = 0
    for batch in _chunks(clean):
        _request(
            "POST",
            f"{client.contacts_table}?on_conflict=domain,email",
            body=batch,
            prefer="resolution=ignore-duplicates,return=minimal",
        )
        written += len(batch)
    return written


def _job_level(title: str) -> str:
    t = (title or "").lower()
    if any(
        k in t
        for k in ("ceo", "owner", "founder", "president", "principal", "partner", "dealer")
    ):
        return "C-Team"
    if "vp" in t or "vice president" in t:
        return "VP"
    if "director" in t:
        return "Director"
    if "manager" in t or "administrator" in t:
        return "Manager"
    return ""


def company_row(
    *,
    client_tag: str,
    domain: str,
    company_name: str = "",
    source: str = "waterfall",
    place: str = "",
    address_city: str = "",
    address_state: str = "",
    email_source_tier: str = "",
    dm_source_tier: str = "",
    source_tier: dict[str, str] | None = None,
    website: str = "",
    dm_lookup_status: str = "",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    tier = dict(source_tier or {})
    if email_source_tier:
        tier["email"] = email_source_tier
    if dm_source_tier:
        tier["dm"] = dm_source_tier
    return {
        "domain": domain,
        "company_name": company_name or None,
        "source": source or "waterfall",
        "place": place or None,
        "address_city": address_city or None,
        "address_state": address_state or None,
        "email_source_tier": email_source_tier or None,
        "dm_source_tier": dm_source_tier or None,
        "source_tier": tier or None,
        "website": website or (f"https://{domain}" if domain else None),
        "dm_lookup_status": dm_lookup_status or None,
        "client_tag": client_tag,
        "updated_at": now,
    }


def contact_row(
    *,
    client_tag: str,
    domain: str,
    first_name: str = "",
    last_name: str = "",
    job_title: str = "",
    email: str = "",
    email_status: str = "",
    cellphone: str = "",
    linkedin_url: str = "",
    contact_city: str = "",
    contact_state: str = "",
    source_tool: str = "",
    source_tier: str = "",
    source_url: str = "",
    confidence: float | None = None,
    place_id: str = "",
    job_level: str = "",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    title = job_title or ""
    return {
        "domain": domain,
        "first_name": first_name or None,
        "last_name": last_name or None,
        "job_title": title or None,
        "job_level": job_level or _job_level(title) or None,
        "email": (email or "").strip().lower() or None,
        "email_status": email_status or None,
        "cellphone": cellphone or None,
        "linkedin_url": linkedin_url or None,
        "contact_city": contact_city or None,
        "contact_state": contact_state or None,
        "source_tool": source_tool or source_tier or None,
        "source_tier": source_tier or source_tool or None,
        "source_url": source_url or None,
        "confidence": confidence,
        "place_id": place_id or None,
        "client_tag": client_tag,
        "updated_at": now,
    }
