"""Title matching, person filters, and client_tag routing."""

from __future__ import annotations

import pytest

from email_waterfall.clients import get_client, normalize_client_tag, parse_target_titles
from email_waterfall.people import looks_like_person, pick_best_person, title_matches, title_rank
from email_waterfall.vendors.base import PersonHit


def test_client_tag_required() -> None:
    with pytest.raises(ValueError, match="required"):
        normalize_client_tag("")
    with pytest.raises(ValueError, match="reserved"):
        normalize_client_tag("shared")
    assert normalize_client_tag("Carlos") == "basco"
    assert normalize_client_tag("vasco") == "basco"
    assert normalize_client_tag("kyle") == "peterson"
    assert normalize_client_tag("goliath") == "goliath"


def test_client_tables_are_isolated() -> None:
    basco = get_client("basco")
    peterson = get_client("peterson")
    assert basco.companies_table == "basco_companies"
    assert basco.contacts_table == "basco_contacts"
    assert peterson.companies_table == "peterson_companies"
    assert peterson.contacts_table == "peterson_contacts"
    assert basco.companies_table != peterson.companies_table


def test_default_titles() -> None:
    basco = get_client("basco")
    titles = parse_target_titles("", basco)
    assert titles[0] == "Service Director"
    assert "GM" in titles


def test_looks_like_person() -> None:
    assert looks_like_person("Jason", "Parrott")
    assert not looks_like_person("Project", "Manager")
    assert not looks_like_person("Service", "Director")
    assert not looks_like_person("Dale Construction", "Corporation")
    assert not looks_like_person("Paragon", "Honda")


def test_basco_title_rank() -> None:
    titles = list(get_client("basco").titles)
    fallback = get_client("basco").fallback_titles
    hi, fb = title_rank("Service Director", titles, fallback)
    mid, _ = title_rank("Warranty Administrator", titles, fallback)
    low, is_fb = title_rank("General Manager", titles, fallback)
    none, _ = title_rank("Lot Porter", titles, fallback)
    assert hi > mid > low > 0
    assert is_fb is True
    assert none == 0
    assert title_matches("Director of Fixed Operations", "Fixed Ops Director")
    assert title_matches("VP of Service", "Vice President of Service")


def test_pick_best_prefers_service_director_over_gm() -> None:
    titles = list(get_client("basco").titles)
    people = [
        PersonHit(first_name="Al", last_name="GM", title="General Manager", source_tier="x"),
        PersonHit(
            first_name="Pat", last_name="Service", title="Service Director", source_tier="x"
        ),
    ]
    # last names "GM" / "Service" might fail looks_like_person? "Service" is a last name
    # looks_like_person rejects first=="service" not last. "GM" last is 2 letters - last_alpha
    # "GM" is 2 chars, OK. first "Al" is 2 chars OK.
    # Wait PersonHit last_name="Service" - ENTITY? no. first Al is fine.
    best = pick_best_person(
        people,
        targets=titles,
        fallback_titles=get_client("basco").fallback_titles,
        require_title_match=True,
    )
    assert best is not None
    assert best.person.first_name == "Pat"


def test_require_title_match_drops_sales() -> None:
    titles = list(get_client("basco").titles)
    people = [
        PersonHit(first_name="Sam", last_name="Sales", title="Sales Manager", source_tier="x")
    ]
    assert (
        pick_best_person(
            people,
            targets=titles,
            fallback_titles=get_client("basco").fallback_titles,
            require_title_match=True,
        )
        is None
    )
    allowed = pick_best_person(
        people,
        targets=titles,
        require_title_match=False,
    )
    assert allowed is not None
