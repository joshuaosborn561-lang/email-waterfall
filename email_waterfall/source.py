"""Read enrichment inputs from a Supabase table. Never return row payloads."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode

from . import supabase_sync
from .config import DEFAULT_SUPABASE_PROJECT, load_settings

PAGE_SIZE = 500
WRITEBACK_COLUMNS = (
    "wf_status",
    "wf_email",
    "wf_email_status",
    "wf_vendor",
    "wf_updated_at",
)
REQUIRED_MAP_FIELDS = ("first_name", "last_name", "company_name")
OPTIONAL_MAP_FIELDS = (
    "domain",
    "title",
    "email",
    "city",
    "state",
    "place_id",
)
MAP_FIELDS = REQUIRED_MAP_FIELDS + OPTIONAL_MAP_FIELDS

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PRED = re.compile(
    r"""
    (?P<col>[A-Za-z_][A-Za-z0-9_]*)
    \s+
    (?:
        (?P<null>is\s+not\s+null|is\s+null)
        |
        (?P<op>=|!=|<>)
        \s+
        (?:'(?P<q>(?:[^']|'')*)'|(?P<num>-?\d+(?:\.\d+)?))
    )
    """,
    re.I | re.X,
)


@dataclass
class TableSource:
    project_id: str
    schema: str = "public"
    table: str = ""
    where: str = ""
    key_column: str = "id"
    column_map: dict[str, str] = field(default_factory=dict)
    limit: int | None = None
    cursor: str | None = None
    writeback: bool = True
    _present_writeback: set[str] | None = field(default=None, repr=False)

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.table}"


def parse_source(raw: Any) -> TableSource:
    if raw is None:
        raise ValueError("source is required when rows are omitted")
    if isinstance(raw, str):
        raw = json.loads(raw) if raw.strip() else {}
    if not isinstance(raw, dict):
        raise ValueError("source must be an object")
    table = str(raw.get("table") or "").strip()
    if not table or not _IDENT.match(table):
        raise ValueError("source.table is required (identifier)")
    schema = str(raw.get("schema") or "public").strip() or "public"
    if not _IDENT.match(schema):
        raise ValueError("source.schema must be an identifier")
    key_column = str(raw.get("key_column") or "id").strip() or "id"
    if not _IDENT.match(key_column):
        raise ValueError("source.key_column must be an identifier")
    mapping_in = raw.get("map") or {}
    if not isinstance(mapping_in, dict):
        raise ValueError("source.map must be an object")
    column_map: dict[str, str] = {}
    for field_name in REQUIRED_MAP_FIELDS:
        col = str(mapping_in.get(field_name) or field_name).strip()
        if not col or not _IDENT.match(col):
            raise ValueError(f"source.map.{field_name} is required (identifier)")
        column_map[field_name] = col
    for field_name in OPTIONAL_MAP_FIELDS:
        if field_name not in mapping_in:
            continue
        col = str(mapping_in.get(field_name) or "").strip()
        if not col:
            continue
        if not _IDENT.match(col):
            raise ValueError(f"source.map.{field_name} must be an identifier")
        column_map[field_name] = col
    limit = raw.get("limit")
    if limit is not None:
        limit = int(limit)
        if limit < 1:
            raise ValueError("source.limit must be >= 1")
    project_id = str(raw.get("project_id") or DEFAULT_SUPABASE_PROJECT).strip()
    if not project_id:
        raise ValueError("source.project_id is required")
    writeback = raw.get("writeback")
    if writeback is None:
        writeback = True
    return TableSource(
        project_id=project_id,
        schema=schema,
        table=table,
        where=str(raw.get("where") or "").strip(),
        key_column=key_column,
        column_map=column_map,
        limit=limit,
        cursor=None if raw.get("cursor") in (None, "") else str(raw.get("cursor")),
        writeback=bool(writeback),
    )


def resolve_credentials(project_id: str) -> tuple[str, str]:
    """Resolve PostgREST URL + service role for a Supabase project ref."""
    cfg = load_settings()
    default_ref = ""
    if cfg.supabase_url:
        host = cfg.supabase_url.split("//", 1)[-1].split("/", 1)[0]
        default_ref = host.split(".")[0]
    env_url = (
        os.environ.get(f"SUPABASE_URL_{project_id}")
        or os.environ.get(f"SUPABASE_URL_{project_id.upper()}")
        or ""
    ).strip()
    env_key = (
        os.environ.get(f"SUPABASE_SERVICE_ROLE_KEY_{project_id}")
        or os.environ.get(f"SUPABASE_SERVICE_ROLE_KEY_{project_id.upper()}")
        or os.environ.get(f"SUPABASE_{project_id.upper()}_SERVICE_ROLE_KEY")
        or ""
    ).strip()
    extra = (os.environ.get("SUPABASE_PROJECT_KEYS") or "").strip()
    if extra and not env_key:
        try:
            parsed = json.loads(extra)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            block = parsed.get(project_id) or {}
            if isinstance(block, dict):
                env_url = env_url or str(block.get("url") or "")
                env_key = str(block.get("service_role_key") or block.get("key") or "")
        elif isinstance(parsed, list):
            for block in parsed:
                if isinstance(block, dict) and str(block.get("id") or "") == project_id:
                    env_url = env_url or str(block.get("url") or "")
                    env_key = str(
                        block.get("service_role_key") or block.get("key") or ""
                    )
                    break
    url = (env_url or f"https://{project_id}.supabase.co").rstrip("/")
    if env_key:
        return url, env_key
    if project_id == default_ref or project_id == DEFAULT_SUPABASE_PROJECT:
        if not cfg.supabase_key:
            raise RuntimeError("Supabase service role key is not configured")
        return cfg.supabase_url.rstrip("/"), cfg.supabase_key
    raise RuntimeError(
        f"No service role key for Supabase project {project_id}. "
        f"Set SUPABASE_SERVICE_ROLE_KEY_{project_id}."
    )


def where_to_filters(where: str) -> list[tuple[str, str]]:
    """Translate a small SQL predicate subset into PostgREST filters."""
    text = (where or "").strip()
    if not text:
        return []
    parts = re.split(r"\s+and\s+", text, flags=re.I)
    out: list[tuple[str, str]] = []
    for part in parts:
        part = part.strip().rstrip(";")
        if not part:
            continue
        m = _PRED.fullmatch(part)
        if not m:
            raise ValueError(
                "source.where only allows AND-combined predicates like "
                "\"wf_status is null\" or \"list_id = 'x'\""
            )
        col = m.group("col")
        if m.group("null"):
            null_op = m.group("null").lower()
            out.append((col, "is.null" if null_op == "is null" else "not.is.null"))
            continue
        op = m.group("op")
        value = m.group("q")
        if value is not None:
            value = value.replace("''", "'")
        else:
            value = m.group("num")
        if op == "=":
            out.append((col, f"eq.{value}"))
        else:
            out.append((col, f"neq.{value}"))
    return out


def _profile_headers(schema: str) -> dict[str, str]:
    if schema == "public":
        return {}
    return {"Accept-Profile": schema, "Content-Profile": schema}


def _select_list(src: TableSource) -> str:
    cols = {src.key_column, *src.column_map.values()}
    return ",".join(sorted(cols))


def fetch_source_rows(src: TableSource) -> list[dict[str, Any]]:
    """Page source rows in batches of 500. Returns mapped rows only (no dump)."""
    url, key = resolve_credentials(src.project_id)
    filters = where_to_filters(src.where)
    mapped: list[dict[str, Any]] = []
    cursor = src.cursor
    remaining = src.limit
    while True:
        page = remaining if remaining is not None and remaining < PAGE_SIZE else PAGE_SIZE
        params: list[tuple[str, str]] = [
            ("select", _select_list(src)),
            ("order", f"{src.key_column}.asc"),
            ("limit", str(page)),
        ]
        for col, expr in filters:
            params.append((col, expr))
        if cursor:
            params.append((src.key_column, f"gt.{cursor}"))
        qs = urlencode(params, safe=".,")
        _status, text = supabase_sync.request_on(
            "GET",
            f"{src.table}?{qs}",
            url=url,
            key=key,
            prefer="return=representation",
            extra_headers=_profile_headers(src.schema),
        )
        rows = json.loads(text) if text else []
        if not isinstance(rows, list) or not rows:
            break
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            item: dict[str, Any] = {"_source_key": raw.get(src.key_column)}
            for field_name, col in src.column_map.items():
                item[field_name] = raw.get(col)
            mapped.append(item)
            cursor = str(raw.get(src.key_column) if raw.get(src.key_column) is not None else cursor)
        if remaining is not None:
            remaining -= len(rows)
            if remaining <= 0:
                break
        if len(rows) < page:
            break
    return mapped


def existing_columns(src: TableSource) -> set[str]:
    if src._present_writeback is not None:
        return src._present_writeback
    url, key = resolve_credentials(src.project_id)
    try:
        supabase_sync.request_on(
            "GET",
            f"{src.table}?select={','.join(WRITEBACK_COLUMNS)}&limit=0",
            url=url,
            key=key,
            extra_headers=_profile_headers(src.schema),
        )
        src._present_writeback = set(WRITEBACK_COLUMNS)
        return src._present_writeback
    except RuntimeError as exc:
        missing = set()
        detail = str(exc).lower()
        for col in WRITEBACK_COLUMNS:
            if col.lower() in detail:
                missing.add(col)
        present = set(WRITEBACK_COLUMNS) - missing if missing else set()
        src._present_writeback = present
        return present


def ensure_writeback_columns(src: TableSource) -> list[str]:
    """Add wf_* columns when writeback=true and they are missing."""
    present = existing_columns(src)
    needed = [c for c in WRITEBACK_COLUMNS if c not in present]
    if not needed:
        return []
    url, key = resolve_credentials(src.project_id)
    stmts = []
    for col in needed:
        typ = "timestamptz" if col.endswith("_at") else "text"
        stmts.append(
            f'alter table "{src.schema}"."{src.table}" add column if not exists "{col}" {typ}'
        )
    sql = "; ".join(stmts)
    try:
        supabase_sync.request_on(
            "POST",
            "rpc/ew_ensure_wf_writeback",
            url=url,
            key=key,
            body={
                "schema_name": src.schema,
                "table_name": src.table,
                "columns": needed,
            },
            prefer="return=representation",
        )
        src._present_writeback = set(WRITEBACK_COLUMNS)
        return needed
    except RuntimeError:
        pass
    try:
        supabase_sync.request_on(
            "POST",
            "rpc/exec_sql",
            url=url,
            key=key,
            body={"sql": sql, "query": sql},
            prefer="return=representation",
        )
        src._present_writeback = set(WRITEBACK_COLUMNS)
        return needed
    except RuntimeError as exc:
        raise RuntimeError(
            "source writeback columns missing "
            f"({', '.join(needed)}) and could not be added automatically: {exc}"
        ) from exc


def writeback_result(
    src: TableSource,
    *,
    key: Any,
    status: str,
    email: str = "",
    email_status: str = "",
    vendor: str = "",
) -> None:
    if key in (None, "") or not src.writeback:
        return
    url, key_auth = resolve_credentials(src.project_id)
    body = {
        "wf_status": status or None,
        "wf_email": (email or "").strip().lower() or None,
        "wf_email_status": email_status or None,
        "wf_vendor": vendor or None,
        "wf_updated_at": datetime.now(timezone.utc).isoformat(),
    }
    present = existing_columns(src)
    body = {k: v for k, v in body.items() if k in present}
    if not body:
        return
    filt = f"{src.key_column}=eq.{quote(str(key), safe='')}"
    supabase_sync.request_on(
        "PATCH",
        f"{src.table}?{filt}",
        url=url,
        key=key_auth,
        body=body,
        prefer="return=minimal",
        extra_headers=_profile_headers(src.schema),
    )
