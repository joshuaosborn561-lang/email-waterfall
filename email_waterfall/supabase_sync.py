"""Write enrichment results to isolated public.{client}_companies / _contacts.

Hard rules:
- client_tag required; never a shared contacts table
- companies upsert on domain, after deduping the batch by domain
- contacts upsert on (client_tag, domain, first_name_key, last_name_key)
  when a person key is present (migration 006 unique index)
- email-only contacts (no name keys) still merge on (domain, email)
- never read or write dl_status, sg_exclude, or skip_* columns
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


def request_on(
    method: str,
    path: str,
    *,
    url: str,
    key: str,
    body: Any = None,
    prefer: str = "return=minimal",
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    """PostgREST call against an explicit Supabase project (url + service key)."""
    endpoint = f"{url.rstrip('/')}/rest/v1/{path}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = _headers(key, prefer=prefer)
    if extra_headers:
        headers.update(extra_headers)
    req = request.Request(
        endpoint,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Supabase {method} {endpoint} failed ({exc.code}): {detail[:500]}"
        ) from exc


def _request(
    method: str,
    path: str,
    *,
    body: Any = None,
    prefer: str = "return=minimal",
) -> tuple[int, str]:
    cfg = supabase_config()
    return request_on(
        method, path, url=cfg["url"], key=cfg["key"], body=body, prefer=prefer
    )


def rpc(name: str, body: dict[str, Any] | None = None) -> Any:
    """Call a PostgREST RPC. Returns parsed JSON or None if unconfigured."""
    if not load_settings().supabase_configured:
        return None
    _status, text = _request(
        "POST",
        f"rpc/{name}",
        body=body or {},
        prefer="return=representation",
    )
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


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


def _person_key(row: dict[str, Any]) -> tuple[str, str, str, str] | None:
    tag = str(row.get("client_tag") or "").strip().lower()
    domain = str(row.get("domain") or "").strip().lower()
    first = str(row.get("first_name_key") or "").strip().lower()
    last = str(row.get("last_name_key") or "").strip().lower()
    if not (tag and domain and first and last):
        return None
    return (tag, domain, first, last)


def _merge_contact(prev: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    merged = dict(prev)
    for key, val in row.items():
        if val not in (None, "", [], {}):
            merged[key] = val
    return merged


def dedupe_contacts_by_person(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per (client_tag, domain, first_name_key, last_name_key)."""
    seen: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str, str]] = []
    for row in rows:
        key = _person_key(row)
        if key is None:
            continue
        if key not in seen:
            order.append(key)
            seen[key] = dict(row)
        else:
            seen[key] = _merge_contact(seen[key], row)
    return [seen[k] for k in order]


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


def _is_missing_relation(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in ("404", "pgrst205", "pgrst116", "does not exist", "not find")
    )


def upsert_companies(client: ClientConfig, rows: list[dict[str, Any]]) -> int:
    rows = dedupe_companies(rows)
    if not rows:
        return 0
    written = 0
    retried = False
    for batch in _chunks(rows):
        try:
            _request(
                "POST",
                f"{client.companies_table}?on_conflict=domain",
                body=batch,
                prefer="resolution=merge-duplicates,return=minimal",
            )
        except RuntimeError as exc:
            if retried or not _is_missing_relation(exc):
                raise
            from .clients import ensure_client

            ensure_client(client.tag, write_supabase=True)
            retried = True
            _request(
                "POST",
                f"{client.companies_table}?on_conflict=domain",
                body=batch,
                prefer="resolution=merge-duplicates,return=minimal",
            )
        written += len(batch)
    return written


def _omit_nones(row: dict[str, Any]) -> dict[str, Any]:
    """Drop nulls so a merge-upsert does not wipe columns we did not set."""
    return {k: v for k, v in row.items() if v is not None}


def insert_contacts(client: ClientConfig, rows: list[dict[str, Any]]) -> int:
    """Fallback insert when the person-key unique index is not yet applied."""
    clean = [_omit_nones(r) for r in rows]
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


def _missing_person_conflict(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "on conflict",
            "no unique",
            "first_name_key",
            "last_name_key",
            "42p10",
            "42703",
        )
    )


def upsert_contacts(client: ClientConfig, rows: list[dict[str, Any]]) -> int:
    """Insert or update contacts.

    Named rows upsert on (client_tag, domain, first_name_key, last_name_key)
    so a re-run cannot duplicate a null-email person. Email-only rows
    (no name keys) still merge on UNIQUE (domain, email).
    """
    named = [_omit_nones(r) for r in dedupe_contacts_by_person(rows)]
    named_keys = {_person_key(r) for r in named}
    leftover = [
        r
        for r in rows
        if _person_key(r) not in named_keys or _person_key(r) is None
    ]
    email_only = [_omit_nones(r) for r in dedupe_contacts_with_email(leftover)]
    written = 0
    if named:
        try:
            written += _upsert_contacts_by_person(client, named)
        except RuntimeError as exc:
            if not _missing_person_conflict(exc):
                raise
            with_email = [r for r in named if (r.get("email") or "").strip()]
            without = [r for r in named if not (r.get("email") or "").strip()]
            if with_email:
                written += _upsert_contacts_with_email(client, with_email)
            if without:
                written += insert_contacts(client, without)
    if email_only:
        written += _upsert_contacts_with_email(client, email_only)
    return written


def _upsert_contacts_by_person(
    client: ClientConfig, rows: list[dict[str, Any]]
) -> int:
    if not rows:
        return 0
    written = 0
    for batch in _chunks(rows):
        _request(
            "POST",
            f"{client.contacts_table}?on_conflict=client_tag,domain,first_name_key,last_name_key",
            body=batch,
            prefer="resolution=merge-duplicates,return=minimal",
        )
        written += len(batch)
    return written


def _upsert_contacts_with_email(
    client: ClientConfig, rows: list[dict[str, Any]]
) -> int:
    if not rows:
        return 0
    written = 0
    for batch in _chunks(rows):
        _request(
            "POST",
            f"{client.contacts_table}?on_conflict=domain,email",
            body=batch,
            prefer="resolution=merge-duplicates,return=minimal",
        )
        written += len(batch)
    return written


def insert_contacts_ignore_conflict(
    client: ClientConfig, rows: list[dict[str, Any]]
) -> int:
    """Update existing (domain, email) rows instead of skipping them."""
    return upsert_contacts(client, rows)


def ensure_contact_columns(client: ClientConfig) -> list[str]:
    """Add line_type (and any future contact columns) on {client}_*contacts."""
    needed = ["line_type", "first_name_key", "last_name_key"]
    try:
        rpc(
            "ew_ensure_contact_person_key",
            {"p_table": client.contacts_table},
        )
    except Exception:
        pass
    try:
        rpc(
            "ew_ensure_contact_columns",
            {"p_table": client.contacts_table, "columns": needed},
        )
        return needed
    except Exception:
        pass
    try:
        rpc(
            "ew_ensure_wf_writeback",
            {
                "schema_name": "public",
                "table_name": client.contacts_table,
                "columns": needed,
            },
        )
        return needed
    except Exception:
        return []


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
    line_type: str = "",
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
        "line_type": (line_type or "").strip().lower() or None,
        "linkedin_url": linkedin_url or None,
        "contact_city": contact_city or None,
        "contact_state": contact_state or None,
        "source_tool": source_tool or source_tier or None,
        "source_tier": source_tier or source_tool or None,
        "source_url": source_url or None,
        "confidence": confidence,
        "place_id": place_id or None,
        "client_tag": client_tag,
        "first_name_key": (first_name or "").strip().lower() or None,
        "last_name_key": (last_name or "").strip().lower() or None,
        "updated_at": now,
    }
