"""Dynamic client_tag registration and isolated table names."""

from __future__ import annotations

import pytest

from email_waterfall.clients import (
    ensure_client,
    get_client,
    list_registered_clients,
    normalize_client_tag,
)


def test_dynamic_tag_uses_wf_tables() -> None:
    client = ensure_client("acme_roofing", write_supabase=False)
    assert client.tag == "acme_roofing"
    assert client.companies_table == "acme_roofing_wf_companies"
    assert client.contacts_table == "acme_roofing_wf_contacts"
    assert client.profile == "owner"
    assert "Owner" in client.titles


def test_service_profile_uses_basco_titles() -> None:
    client = ensure_client("newdealer", profile="service", write_supabase=False)
    assert client.profile == "service"
    assert client.titles[0] == "Service Director"
    assert client.companies_table == "newdealer_wf_companies"


def test_custom_titles_override() -> None:
    client = ensure_client(
        "itshop",
        target_titles="CIO, IT Director, CISO",
        write_supabase=False,
    )
    assert list(client.titles) == ["CIO", "IT Director", "CISO"]


def test_legacy_basco_peterson_table_names() -> None:
    assert get_client("basco").companies_table == "basco_companies"
    assert get_client("peterson").contacts_table == "peterson_contacts"
    assert get_client("goliath").companies_table == "goliath_wf_companies"
    assert get_client("salesglider").contacts_table == "salesglider_wf_contacts"


def test_get_client_auto_ensures_unknown_tag() -> None:
    client = get_client("brand_new_shop")
    assert client.tag == "brand_new_shop"
    assert client.companies_table == "brand_new_shop_wf_companies"
    tags = {c.tag for c in list_registered_clients()}
    assert "brand_new_shop" in tags


def test_reserved_and_invalid_tags() -> None:
    with pytest.raises(ValueError, match="required"):
        normalize_client_tag("")
    with pytest.raises(ValueError, match="reserved"):
        normalize_client_tag("public")
    with pytest.raises(ValueError, match="snake_case"):
        normalize_client_tag("123bad")
