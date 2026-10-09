"""approve_cost_usd: estimate-then-refuse and running-spend hard stop."""

from __future__ import annotations

from email_waterfall import waterfall
from email_waterfall.vendors.base import EmailHit
from tests.test_waterfall import _patch_clients, _patch_writes, _vendor


def test_estimate_includes_usd_and_default_ceiling(monkeypatch) -> None:
    out = waterfall.enrich_waterfall(
        [
            {
                "domain": "roofco.com",
                "first_name": "Jane",
                "last_name": "Smith",
                "company_name": "Roof Co",
            }
        ]
        * 10,
        client_tag="peterson",
        need="email",
        estimate_only=True,
        write_supabase=False,
    )
    assert out["max_tier"] == "prospeo"
    assert out["approve_cost_usd"] == waterfall.DEFAULT_APPROVE_COST_USD
    assert out["estimated_cost_usd"] > 0
    assert "usd_est" in out["estimate"]["aiark"]
    assert "usd_est" in out["estimate"]["prospeo"]
    assert "fullenrich" not in out["estimate"]


def test_refuse_when_estimate_exceeds_ceiling(monkeypatch) -> None:
    gl = _vendor(email=None)
    ark = _vendor(email=EmailHit(email="jane@roofco.com", source_tier="aiark"))
    _patch_clients(monkeypatch, gl=gl, ark=ark, fe=_vendor(enabled=False))
    _patch_writes(monkeypatch, {})
    rows = [
        {
            "domain": f"co{i}.com",
            "first_name": "Jane",
            "last_name": "Smith",
            "company_name": "Roof Co",
        }
        for i in range(50)
    ]
    out = waterfall.enrich_waterfall(
        rows,
        client_tag="peterson",
        need="email",
        max_tier="prospeo",
        approve_cost_usd=0.01,
        write_supabase=True,
    )
    assert out["ok"] is False
    assert out["status"] == "refused_over_ceiling"
    assert out["estimated_cost_usd"] > 0.01
    assert out["spend"] == 0
    gl.find_email.assert_not_called()
    ark.find_email.assert_not_called()


def test_estimate_only_never_refuses(monkeypatch) -> None:
    gl = _vendor(email=None)
    ark = _vendor(email=EmailHit(email="jane@roofco.com", source_tier="aiark"))
    _patch_clients(monkeypatch, gl=gl, ark=ark, fe=_vendor(enabled=False))
    out = waterfall.enrich_waterfall(
        [
            {
                "domain": f"co{i}.com",
                "first_name": "Jane",
                "last_name": "Smith",
                "company_name": "Roof Co",
            }
            for i in range(50)
        ],
        client_tag="peterson",
        need="email",
        estimate_only=True,
        approve_cost_usd=0.01,
        write_supabase=False,
    )
    assert out["estimate_only"] is True
    assert out.get("would_refuse") is True
    assert out["status"] == "refused_over_ceiling"
    assert "ok" not in out or out.get("ok") is not False
    gl.find_email.assert_not_called()
    ark.find_email.assert_not_called()


def test_bump_attempt_hard_stops_when_next_would_exceed() -> None:
    wf = waterfall.Waterfall(
        getleads=_vendor(enabled=False),
        smartlead=_vendor(enabled=False),
        ai_ark=_vendor(enabled=False),
        prospeo=_vendor(enabled=False),
        fullenrich=_vendor(enabled=False),
        veriphone=_vendor(enabled=False),
        need="email",
        max_tier="aiark",
        approve_cost_usd=0.006,
    )
    row1 = {"domain": "a.com"}
    row2 = {"domain": "b.com"}
    assert wf._bump_attempt("aiark", row1) is True
    assert wf.spend_usd == waterfall.attempt_cost_usd("aiark", credits=1.0)
    assert wf._bump_attempt("aiark", row2) is False
    assert wf.stopped_at_ceiling is True
    assert wf._allowed("aiark") is False


def test_running_spend_stops_at_ceiling(monkeypatch) -> None:
    """Understated quote lets the job start; running spend then hard-stops."""
    import email_waterfall._wf.exec as exec_mod

    real_estimate = exec_mod.estimate_waterfall

    def cheap_quote(*args, **kwargs):
        out = real_estimate(*args, **kwargs)
        out["estimated_cost_usd"] = 0.001
        out.pop("would_refuse", None)
        if out.get("status") == "refused_over_ceiling":
            out.pop("status", None)
        return out

    monkeypatch.setattr(exec_mod, "estimate_waterfall", cheap_quote)
    sink: dict = {}
    ark = _vendor(email=None)
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=ark,
        fe=_vendor(enabled=False),
        prospeo=_vendor(enabled=False),
    )
    _patch_writes(monkeypatch, sink)
    # AI Ark email-only is 1.0 cr × $0.003667 ≈ $0.0037 per row.
    # Ceiling 0.006 lets the first paid attempt through, then stops.
    out = waterfall.enrich_waterfall(
        [
            {
                "domain": "a.com",
                "first_name": "Jane",
                "last_name": "One",
            },
            {
                "domain": "b.com",
                "first_name": "Jane",
                "last_name": "Two",
            },
            {
                "domain": "c.com",
                "first_name": "Jane",
                "last_name": "Three",
            },
        ],
        client_tag="peterson",
        need="email",
        max_tier="aiark",
        approve_cost_usd=0.006,
        write_supabase=True,
        parallel=False,
    )
    assert out["status"] == "stopped_at_ceiling"
    assert out["spend"] > 0
    assert out["spend"] <= 0.006
    assert ark.find_email.call_count == 1


def test_enrich_one_refuses_over_tiny_ceiling(monkeypatch) -> None:
    ark = _vendor(email=EmailHit(email="jane@roofco.com", source_tier="aiark"))
    _patch_clients(
        monkeypatch,
        gl=_vendor(email=None),
        ark=ark,
        fe=_vendor(enabled=True),
        prospeo=_vendor(enabled=True),
    )
    hit = waterfall.enrich_one_person(
        client_tag="replyhandler",
        first_name="Jane",
        last_name="Smith",
        domain="roofco.com",
        need="both",
        max_tier="fullenrich",
        approve_cost_usd=0.001,
        write_supabase=False,
    )
    assert hit["ok"] is False
    assert hit["status"] == "refused_over_ceiling"
    ark.find_email.assert_not_called()
