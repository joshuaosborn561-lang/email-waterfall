"""Table source: WHERE parser, map select list, paging, writeback."""

from __future__ import annotations

import pytest

from email_waterfall import source as table_source
from email_waterfall import waterfall
from email_waterfall.vendors.base import EmailHit


def test_where_is_null() -> None:
    assert table_source.where_to_filters("wf_status is null") == [
        ("wf_status", "is.null")
    ]


def test_where_eq_and_is_not_null() -> None:
    filters = table_source.where_to_filters(
        "list_id = 'ew_nonprofit_officers_v1' AND email is not null"
    )
    assert filters == [
        ("list_id", "eq.ew_nonprofit_officers_v1"),
        ("email", "not.is.null"),
    ]


def test_where_rejects_or() -> None:
    with pytest.raises(ValueError, match="source.where"):
        table_source.where_to_filters("wf_status is null OR list_id = 'x'")


def test_parse_source_defaults_required_map_only() -> None:
    src = table_source.parse_source(
        {
            "project_id": "kemvxzhcxvynmoutwdrh",
            "table": "ew_names_ready",
            "where": "wf_status is null",
            "map": {
                "first_name": "first_name",
                "last_name": "last_name",
                "company_name": "company_name",
            },
        }
    )
    assert src.column_map == {
        "first_name": "first_name",
        "last_name": "last_name",
        "company_name": "company_name",
    }
    assert "domain" not in src.column_map
    assert set(table_source._select_list(src).split(",")) == {
        "id",
        "first_name",
        "last_name",
        "company_name",
    }


def test_parse_source_omitted_map_defaults_required_fields() -> None:
    src = table_source.parse_source({"table": "ew_names_ready"})
    assert src.schema == "public"
    assert src.key_column == "id"
    assert src.column_map == {
        "first_name": "first_name",
        "last_name": "last_name",
        "company_name": "company_name",
    }


def test_rows_and_source_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="not both"):
        waterfall.enrich_waterfall(
            [{"first_name": "A", "last_name": "B", "company_name": "C"}],
            client_tag="peterson",
            source={"table": "ew_names_ready"},
            write_supabase=False,
            estimate_only=True,
        )


def test_source_or_rows_required() -> None:
    with pytest.raises(ValueError, match="rows or source"):
        waterfall.enrich_waterfall(
            None, client_tag="peterson", write_supabase=False, estimate_only=True
        )


def test_parse_source_table_qualified() -> None:
    src = table_source.parse_source({"table": "client_peterson.email_resolution"})
    assert src.schema == "client_peterson"
    assert src.table == "email_resolution"


def test_source_table_and_where_top_level(monkeypatch) -> None:
    fetched = [
        {
            "_source_key": 1,
            "first_name": "Jane",
            "last_name": "Doe",
            "company_name": "Helping Hands",
            "domain": "helpinghands.org",
        }
    ]
    monkeypatch.setattr(waterfall.table_source, "fetch_source_rows", lambda src: fetched)
    out = waterfall.enrich_waterfall(
        client_tag="peterson",
        need="email",
        source_table="client_peterson.email_resolution",
        where="candidate_email is null",
        estimate_only=True,
        write_supabase=False,
    )
    assert out["rows_in"] == 1
    assert out["modes"]["domain"] == 1
    assert out["spend"] == 0


def test_discover_maps_owner_title_and_candidate_email(monkeypatch) -> None:
    src = table_source.parse_source({"table": "client_peterson.email_resolution"})
    monkeypatch.setattr(
        table_source,
        "list_table_columns",
        lambda _src: {
            "id",
            "first_name",
            "last_name",
            "company_name",
            "domain",
            "owner_title",
            "candidate_email",
            "city",
        },
    )
    table_source.discover_column_map(src)
    assert src.column_map["title"] == "owner_title"
    assert src.column_map["email"] == "candidate_email"
    assert src.column_map["domain"] == "domain"


def test_fetch_pages_500(monkeypatch) -> None:
    src = table_source.parse_source(
        {
            "project_id": "kemvxzhcxvynmoutwdrh",
            "table": "ew_names_ready",
            "map": {
                "first_name": "first_name",
                "last_name": "last_name",
                "company_name": "company_name",
            },
        }
    )
    pages: list[str] = []

    def fake_request(method, path, **kwargs):
        pages.append(path)
        if "gt.500" in path:
            return 200, "[]"
        rows = [
            {
                "id": i,
                "first_name": "A",
                "last_name": "B",
                "company_name": "Org",
            }
            for i in range(1, 501)
        ]
        import json

        return 200, json.dumps(rows)

    monkeypatch.setattr(table_source, "resolve_credentials", lambda pid: ("https://x", "k"))
    monkeypatch.setattr(table_source, "list_table_columns", lambda _src: None)
    monkeypatch.setattr(table_source.supabase_sync, "request_on", fake_request)
    mapped = table_source.fetch_source_rows(src)
    assert len(mapped) == 500
    assert mapped[0]["_source_key"] == 1
    assert mapped[0]["company_name"] == "Org"
    assert "limit=500" in pages[0]
    assert any("gt.500" in p for p in pages)


def test_writeback_patches_wf_columns(monkeypatch) -> None:
    src = table_source.parse_source(
        {"project_id": "kemvxzhcxvynmoutwdrh", "table": "ew_names_ready"}
    )
    src._present_writeback = set(table_source.WRITEBACK_COLUMNS)
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs.get("body") or {}))
        return 200, ""

    monkeypatch.setattr(table_source, "resolve_credentials", lambda pid: ("https://x", "k"))
    monkeypatch.setattr(table_source.supabase_sync, "request_on", fake_request)
    table_source.writeback_result(
        src,
        key=42,
        status="found",
        email="jane@org.org",
        email_status="found",
        vendor="leadmagic",
    )
    assert calls[0][0] == "PATCH"
    assert "id=eq.42" in calls[0][1]
    body = calls[0][2]
    assert body["wf_email"] == "jane@org.org"
    assert body["wf_status"] == "found"
    assert body["wf_vendor"] == "leadmagic"
    assert "wf_updated_at" in body


def test_source_enrich_writes_back(monkeypatch) -> None:
    from tests.test_waterfall import _patch_clients, _patch_writes, _vendor

    sink: dict = {}
    lm = _vendor(
        email=EmailHit(email="jane@org.org", source_tier="leadmagic", status="valid")
    )
    _patch_clients(
        monkeypatch,
        gl=_vendor(enabled=True),
        ark=_vendor(email=None),
        lm=lm,
        fe=_vendor(enabled=False),
    )
    _patch_writes(monkeypatch, sink)
    writebacks: list[dict] = []

    def fake_writeback(src, **kwargs):
        writebacks.append(kwargs)

    monkeypatch.setattr(
        waterfall.table_source,
        "fetch_source_rows",
        lambda src: [
            {
                "_source_key": 9,
                "first_name": "Jane",
                "last_name": "Doe",
                "company_name": "Helping Hands",
            }
        ],
    )
    monkeypatch.setattr(waterfall.table_source, "ensure_writeback_columns", lambda src: [])
    monkeypatch.setattr(waterfall.table_source, "writeback_result", fake_writeback)

    out = waterfall.enrich_waterfall(
        client_tag="peterson",
        need="email",
        max_tier="leadmagic",
        write_supabase=True,
        source={
            "project_id": "kemvxzhcxvynmoutwdrh",
            "table": "ew_names_ready",
            "where": "wf_status is null",
            "map": {
                "first_name": "first_name",
                "last_name": "last_name",
                "company_name": "company_name",
            },
        },
    )
    assert out["rows_in"] == 1
    assert out["emails_found"] == 1
    assert "items" not in out and "rows" not in out
    assert writebacks[0]["key"] == 9
    assert writebacks[0]["email"] == "jane@org.org"
    assert writebacks[0]["vendor"] == "leadmagic"
