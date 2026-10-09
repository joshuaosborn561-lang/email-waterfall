"""Name + company rows: skip domain-only tiers; FullEnrich gets verbatim company."""

from __future__ import annotations

from email_waterfall import waterfall
from email_waterfall.vendors.base import EmailHit
from email_waterfall.vendors.fullenrich import FullEnrichClient
from tests.test_waterfall import _patch_clients, _patch_writes, _vendor


def test_name_company_not_rejected(monkeypatch) -> None:
    sink: dict = {}
    ark = _vendor(
        email=EmailHit(email="jane@helpinghands.org", source_tier="aiark")
    )
    gl = _vendor(enabled=True)
    sl = _vendor(enabled=True)
    _patch_clients(
        monkeypatch,
        gl=gl,
        ark=ark,
        fe=_vendor(enabled=False),
        smartlead=sl,
    )
    _patch_writes(monkeypatch, sink)

    out = waterfall.enrich_waterfall(
        [
            {
                "first_name": "Jane",
                "last_name": "Doe",
                "company_name": "Helping Hands Inc",
            }
        ],
        client_tag="peterson",
        need="email",
        max_tier="aiark",
        write_supabase=True,
    )
    assert out["rows_in"] == 1
    assert out["modes"]["name_company"] == 1
    assert out["emails_found"] == 1
    gl.find_email.assert_called()
    sl.find_email.assert_not_called()
    ark.find_email.assert_called()
    args, kwargs = ark.find_email.call_args
    assert args[0] == "Jane"
    assert args[1] == "Doe"


def test_name_company_fills_domain_for_later_tiers(monkeypatch) -> None:
    sink: dict = {}
    ark = _vendor(email=None)
    prospeo = _vendor(
        email=EmailHit(
            email="jane@helpinghands.org",
            source_tier="prospeo",
            raw={"domain": "helpinghands.org"},
        )
    )
    fe = _vendor(enabled=True)
    _patch_clients(
        monkeypatch,
        gl=_vendor(enabled=True),
        ark=ark,
        fe=fe,
        prospeo=prospeo,
    )
    _patch_writes(monkeypatch, sink)

    out = waterfall.enrich_waterfall(
        [
            {
                "first_name": "Jane",
                "last_name": "Doe",
                "company_name": "Helping Hands Inc",
            }
        ],
        client_tag="peterson",
        need="email",
        max_tier="fullenrich",
        write_supabase=True,
    )
    assert out["emails_found"] == 1
    assert sink["companies"][0]["domain"] == "helpinghands.org"
    fe.find_email.assert_not_called()
    fe.find_email_bulk.assert_not_called()


def test_fullenrich_company_name_is_verbatim(monkeypatch) -> None:
    client = FullEnrichClient(api_key="fe_test")
    captured: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {}

    def fake_post(tier, url, json=None, **kwargs):
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr("email_waterfall.vendors.fullenrich.http_client.post", fake_post)
    client.find_email_bulk(
        [
            {
                "first_name": "Jane",
                "last_name": "Doe",
                "domain": "",
                "company_name": "Peterson Earthworks Inc",
            }
        ],
        max_wait=0,
        poll_seconds=0,
    )
    payload = captured["json"]["data"][0]
    assert payload["company_name"] == "Peterson Earthworks Inc"
    assert "domain" not in payload
    assert payload["company_name"] != "petersonearthworks.com"


def test_fullenrich_does_not_substitute_domain_for_company(monkeypatch) -> None:
    client = FullEnrichClient(api_key="fe_test")
    captured: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {}

    def fake_post(tier, url, json=None, **kwargs):
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr("email_waterfall.vendors.fullenrich.http_client.post", fake_post)
    client.find_email_bulk(
        [
            {
                "first_name": "Jane",
                "last_name": "Doe",
                "domain": "roofco.com",
                "company_name": "Roof Co LLC",
            }
        ],
        max_wait=0,
        poll_seconds=0,
    )
    payload = captured["json"]["data"][0]
    assert payload["company_name"] == "Roof Co LLC"
    assert payload["domain"] == "roofco.com"


def test_aiark_people_search_uses_company_name(monkeypatch) -> None:
    from email_waterfall.vendors.ai_ark import AiArkClient

    client = AiArkClient(api_key="tok")

    def fake_post(path, body):
        assert path.endswith("/v1/people")
        assert "domain" not in body.get("account", {})
        assert body["account"]["name"]["any"]["include"]["content"] == [
            "Helping Hands Inc"
        ]
        assert "Jane Doe" in body["contact"]["fullName"]["any"]["include"]["content"]
        return 200, {"content": []}

    monkeypatch.setattr(client, "_post", fake_post)
    assert (
        client.find_people("", company_name="Helping Hands Inc", full_name="Jane Doe")
        == []
    )


def test_empty_rows_still_zero() -> None:
    out = waterfall.enrich_waterfall([], client_tag="peterson", write_supabase=False)
    assert out["rows_in"] == 0
