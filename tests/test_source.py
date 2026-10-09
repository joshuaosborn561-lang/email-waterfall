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


def test_coerce_source_table_name_string() -> None:
    assert table_source.coerce_source("client_peterson.email_resolution") == {
        "table": "client_peterson.email_resolution"
    }


def test_coerce_source_and_source_table_both_table_names() -> None:
    assert table_source.coerce_source(
        "client_peterson.email_resolution",
        source_table="client_peterson.email_resolution",
        where="wf_status is null",
    ) == {
        "table": "client_peterson.email_resolution",
        "where": "wf_status is null",
    }


def test_coerce_source_table_object() -> None:
    assert table_source.coerce_source(
        source_table={
            "table": "client_peterson.email_resolution",
            "where": "wf_status is null",
        }
    ) == {
        "table": "client_peterson.email_resolution",
        "where": "wf_status is null",
    }


def test_coerce_source_json_object_string() -> None:
    assert table_source.coerce_source(
        '{"table":"client_peterson.email_resolution","where":"wf_status is null"}'
    ) == {
        "table": "client_peterson.email_resolution",
        "where": "wf_status is null",
    }


def test_coerce_source_null_companions() -> None:
    assert table_source.coerce_source(
        None, source_table="client_peterson.email_resolution", where=None
    ) == {"table": "client_peterson.email_resolution"}
    assert table_source.coerce_source(None, source_table=None, where=None) is None


def test_empty_rows_do_not_block_source_table(monkeypatch) -> None:
    """MCP hosts often send rows=[] / source={} alongside source_table."""
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
        [],
        client_tag="peterson",
        need="email",
        source={},
        source_table="client_peterson.email_resolution",
        where="wf_status is null",
        estimate_only=True,
        write_supabase=False,
    )
    assert out["rows_in"] == 1
    assert out["spend"] == 0


def test_domain_only_table_skips_missing_name_columns(monkeypatch) -> None:
    src = table_source.parse_source({"table": "domain_only"})
    monkeypatch.setattr(
        table_source,
        "list_table_columns",
        lambda s: {"id", "company_name", "city", "state", "domain"},
    )
    captured: dict[str, str] = {}

    def fake_rest(src, **kwargs):
        captured["select"] = table_source._select_list(src)
        return [
            {
                "id": 1,
                "company_name": "Acme",
                "city": "Dallas",
                "state": "TX",
                "domain": "acme.com",
            }
        ]

    monkeypatch.setattr(table_source, "_fetch_page_rest", fake_rest)
    monkeypatch.setattr(
        table_source, "resolve_credentials", lambda pid: ("https://x.supabase.co", "k")
    )
    rows = table_source.fetch_source_rows(src)
    assert "first_name" not in captured["select"].split(",")
    assert "last_name" not in captured["select"].split(",")
    assert "domain" in captured["select"]
    assert "company_name" in captured["select"]
    assert rows[0]["domain"] == "acme.com"
    assert rows[0].get("first_name") in (None, "")


def test_parse_source_table_qualified() -> None:
    src = table_source.parse_source({"table": "client_peterson.email_resolution"})
    assert src.schema == "client_peterson"
    assert src.table == "email_resolution"


def test_source_string_table_name_reaches_fetch(monkeypatch) -> None:
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
        source="client_peterson.email_resolution",
        source_table=None,
        where="wf_status is null",
        estimate_only=True,
        write_supabase=False,
    )
    assert out["rows_in"] == 1
    assert out["spend"] == 0


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


def test_discover_maps_phone_aliases(monkeypatch) -> None:
    src = table_source.parse_source({"table": "client_peterson.email_resolution"})
    monkeypatch.setattr(
        table_source,
        "list_table_columns",
        lambda _src: {
            "id",
            "first_name",
            "last_name",
            "company_name",
            "cellphone",
            "wf_phone",
        },
    )
    table_source.discover_column_map(src)
    assert src.column_map["phone"] == "cellphone"


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
    src._present_writeback = set(table_source.ALL_WRITEBACK_COLUMNS)
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
        vendor="prospeo",
    )
    assert calls[0][0] == "PATCH"
    assert "id=eq.42" in calls[0][1]
    body = calls[0][2]
    assert body["wf_email"] == "jane@org.org"
    assert body["wf_status"] == "found"
    assert body["wf_vendor"] == "prospeo"
    assert "dl_status" not in body
    assert "candidate_email" not in body
    assert "dl_provider" not in body
    assert "wf_updated_at" in body
    assert "wf_phone" not in body


def test_writeback_patches_wf_phone_and_type(monkeypatch) -> None:
    src = table_source.parse_source(
        {"project_id": "kemvxzhcxvynmoutwdrh", "table": "ew_names_ready"}
    )
    src._present_writeback = set(table_source.ALL_WRITEBACK_COLUMNS)
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
        email="",
        email_status="not_found",
        vendor="aiark",
        phone="+19725550111",
        phone_type="mobile",
    )
    body = calls[0][2]
    assert body["wf_phone"] == "+19725550111"
    assert body["wf_phone_type"] == "mobile"
    assert body["wf_status"] == "found"


def test_writeback_skips_when_only_forbidden_columns_present(monkeypatch) -> None:
    src = table_source.parse_source(
        {"table": "client_peterson.email_resolution"}
    )
    src._present_writeback = {"dl_status", "candidate_email", "dl_provider"}
    calls: list[tuple[str, str, dict]] = []

    def fake_rpc(path, body, **kwargs):
        calls.append((path, body))
        return {}

    monkeypatch.setattr(table_source, "resolve_credentials", lambda pid: ("https://x", "k"))
    monkeypatch.setattr(table_source, "_rpc_json", fake_rpc)
    table_source.writeback_result(
        src,
        key=7,
        status="not_found",
        email="",
        email_status="not_found",
        vendor="smartlead",
    )
    assert calls == []


def test_where_rejects_forbidden_columns() -> None:
    with pytest.raises(ValueError, match="dl_status"):
        table_source.where_to_filters("dl_status is null")
    with pytest.raises(ValueError, match="sg_exclude"):
        table_source.where_to_filters("sg_exclude = '1'")
    with pytest.raises(ValueError, match="skip_"):
        table_source.where_to_filters("skip_email is null")


def test_source_enrich_writes_back(monkeypatch) -> None:
    from tests.test_waterfall import _patch_clients, _patch_writes, _vendor

    sink: dict = {}
    ark = _vendor(
        email=EmailHit(email="jane@org.org", source_tier="aiark", status="valid")
    )
    _patch_clients(
        monkeypatch,
        gl=_vendor(enabled=True),
        ark=ark,
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
        max_tier="aiark",
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
    assert writebacks[0]["vendor"] == "aiark"
