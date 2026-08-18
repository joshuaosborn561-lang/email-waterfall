"""Company-domain dedupe and contact email split."""

from __future__ import annotations

from email_waterfall.supabase_sync import (
    company_row,
    contact_row,
    dedupe_companies,
    dedupe_contacts_with_email,
)


def test_dedupe_companies_by_domain() -> None:
    rows = [
        company_row(client_tag="basco", domain="paragonhonda.com", company_name="A"),
        company_row(
            client_tag="basco",
            domain="PARAGONHONDA.COM",
            company_name="Paragon Honda",
            dm_source_tier="leadmagic",
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
    assert para["dm_source_tier"] == "leadmagic"


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
