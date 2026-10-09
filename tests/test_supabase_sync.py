"""Company-domain dedupe and contact email split."""

from __future__ import annotations

from email_waterfall.clients import CLIENTS
from email_waterfall.supabase_sync import (
    company_row,
    contact_row,
    dedupe_companies,
    dedupe_contacts_by_person,
    dedupe_contacts_with_email,
    upsert_contacts,
)


def test_dedupe_companies_by_domain() -> None:
    rows = [
        company_row(client_tag="basco", domain="paragonhonda.com", company_name="A"),
        company_row(
            client_tag="basco",
            domain="PARAGONHONDA.COM",
            company_name="Paragon Honda",
            dm_source_tier="prospeo",
            dm_lookup_status="found",
        ),
        company_row(client_tag="basco", domain="other.com", company_name="Other"),
    ]
    out = dedupe_companies(rows)
    domains = [r["domain"] for r in out]
    assert domains == ["paragonhonda.com", "other.com"]
    para = out[0]
    assert para["company_name"] == "Paragon Honda"
    assert para["dm_lookup_status"] == "found"
    assert para["dm_source_tier"] == "prospeo"


def test_dedupe_keeps_found_status() -> None:
    a = company_row(
        client_tag="peterson",
        domain="acme.com",
        dm_lookup_status="found",
        dm_source_tier="aiark",
    )
    b = company_row(
        client_tag="peterson",
        domain="acme.com",
        dm_lookup_status="not_found",
    )
    out = dedupe_companies([a, b])
    assert len(out) == 1
    assert out[0]["dm_lookup_status"] == "found"


def test_contact_email_dedupe() -> None:
    rows = [
        contact_row(
            client_tag="basco",
            domain="x.com",
            first_name="A",
            last_name="B",
            email="a@x.com",
        ),
        contact_row(
            client_tag="basco",
            domain="x.com",
            first_name="A",
            last_name="B",
            email="A@x.com",
            job_title="Service Director",
        ),
        contact_row(
            client_tag="basco",
            domain="x.com",
            first_name="No",
            last_name="Mail",
            email="",
        ),
    ]
    with_email = [r for r in rows if r.get("email")]
    out = dedupe_contacts_with_email(with_email)
    assert len(out) == 1
    assert out[0]["job_title"] == "Service Director"


def test_contact_row_includes_line_type() -> None:
    row = contact_row(
        client_tag="peterson",
        domain="roofco.com",
        first_name="Jane",
        last_name="Smith",
        cellphone="+19725550111",
        line_type="Mobile",
    )
    assert row["cellphone"] == "+19725550111"
    assert row["line_type"] == "mobile"


def test_upsert_contacts_merges_existing_email_rows(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, str(kwargs.get("prefer") or "")))
        return 200, ""

    monkeypatch.setattr(
        "email_waterfall.supabase_sync._request", fake_request
    )
    written = upsert_contacts(
        CLIENTS["peterson"],
        [
            contact_row(
                client_tag="peterson",
                domain="roofco.com",
                first_name="Jane",
                last_name="Smith",
                email="jane@roofco.com",
                cellphone="+19725550111",
                line_type="mobile",
            )
        ],
    )
    assert written == 1
    assert "on_conflict=client_tag,domain,first_name_key,last_name_key" in calls[0][1]
    assert "merge-duplicates" in calls[0][2]
    assert "ignore-duplicates" not in calls[0][2]


def test_contact_row_includes_person_key() -> None:
    row = contact_row(
        client_tag="peterson",
        domain="roofco.com",
        first_name="Jane",
        last_name="Smith",
    )
    assert row["first_name_key"] == "jane"
    assert row["last_name_key"] == "smith"


def test_null_email_upsert_is_idempotent(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs.get("body")))
        return 200, ""

    monkeypatch.setattr("email_waterfall.supabase_sync._request", fake_request)
    row = contact_row(
        client_tag="goliath",
        domain="acme.com",
        first_name="Pat",
        last_name="Lee",
        email="",
    )
    first = upsert_contacts(CLIENTS["goliath"], [row])
    second = upsert_contacts(CLIENTS["goliath"], [row])
    assert first == 1
    assert second == 1
    assert all(
        "on_conflict=client_tag,domain,first_name_key,last_name_key" in c[1]
        for c in calls
    )
    assert dedupe_contacts_by_person([row, row]) == [row]
