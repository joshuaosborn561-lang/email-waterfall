"""Call-ready enrichment: one verified phone, the right LinkedIn, ICP fit and
decision-maker verdicts for one contact, under a hard per-lead USD cap.

Order (each step stops spending once the cap would be crossed):
  1. Phone: FullEnrich mobile first, then caller-supplied phones (Smartlead /
     HeyReach lead fields), then the existing waterfall phone path
     (getleads -> AI Ark -> Prospeo). Every candidate goes through Veriphone
     mode=current. Ranked mobile > fixed line > voip; only phone_valid counts
     as verified.
  2. LinkedIn: candidates from AI Ark people search, Prospeo and FullEnrich
     payloads, scored on name, company/domain, title and location. Below the
     threshold -> linkedin_confidence='review'.
  3. ICP fit + decision maker: AI verdicts from ReplyHandler's
     /internal/lead-verdict (same ICP grader and Fireflies-built client ICP
     the reply cards use). Sales reps / coordinators are never DMs.
  4. Results cached per person (Supabase ew_call_ready_cache).

Never logs or returns a key. Never writes dl_status / sg_exclude / skip_*.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from difflib import SequenceMatcher
from typing import Any, Callable
from urllib import error as urlerror
from urllib import request as urlrequest

from email_waterfall._wf.const import USD_PER_CREDIT

HARD_CAP_USD = 5.0
CACHE_TABLE = "ew_call_ready_cache"
CACHE_TTL_DAYS = 60
LINKEDIN_THRESHOLD = 0.70

# Booked prices (Josh's contract rates). FullEnrich mobile = 10 credits.
FULLENRICH_PHONE_CREDITS = 10.0
FULLENRICH_LINKEDIN_CREDITS = 1.0


def _usd(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


def fullenrich_phone_usd() -> float:
    return round(FULLENRICH_PHONE_CREDITS * USD_PER_CREDIT["fullenrich"], 6)


def veriphone_usd() -> float:
    # Contract rate is $0 (USD_PER_CREDIT); override if the plan changes.
    return _usd("VERIPHONE_CURRENT_USD", USD_PER_CREDIT.get("veriphone", 0.0))


def verdict_usd() -> float:
    return _usd("CALL_READY_VERDICT_USD", 0.004)


PHONE_TYPE_RANK = {"mobile": 0, "fixed_line_or_mobile": 1, "fixed_line": 2,
                   "landline": 2, "toll_free": 4, "voip": 3}

_DM_NO = re.compile(
    r"\b(sales|account executive|account manager|business development|bdr|sdr|"
    r"inside sales|sales rep|representative|coordinator|assistant|intern|"
    r"receptionist|clerk|trainee)\b",
    re.I,
)
_DM_EXEC = re.compile(
    r"\b(owner|founder|ceo|chief|president|principal|partner|managing director|"
    r"general manager|vp|vice president|head of|director|c[eoft]o)\b",
    re.I,
)


class Budget:
    def __init__(self, cap: float):
        self.cap = min(float(cap), HARD_CAP_USD)
        self.spent = 0.0
        self.skipped: list[str] = []

    def can(self, usd: float) -> bool:
        return self.spent + usd <= self.cap + 1e-9

    def charge(self, usd: float) -> None:
        self.spent = round(self.spent + max(0.0, usd), 6)


# ---------------------------------------------------------------- helpers

def _clean(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _digits(p: str) -> str:
    return "".join(c for c in str(p or "") if c.isdigit())


def norm_phone(p: str) -> str:
    d = _digits(p)
    if len(d) == 10:
        d = "1" + d
    return d


def norm_linkedin(url: str) -> str:
    m = re.search(r"linkedin\.com/in/([^/?#\s\"']+)", str(url or ""), re.I)
    if not m:
        return ""
    return f"https://www.linkedin.com/in/{m.group(1).strip().lower().rstrip('/')}"


def person_key(row: dict[str, Any]) -> str:
    li = norm_linkedin(row.get("linkedin_url") or "")
    email = _clean(row.get("email")).lower()
    name = _clean(row.get("full_name") or f"{row.get('first_name','')} {row.get('last_name','')}").lower()
    dom = _clean(row.get("domain")).lower() or _clean(row.get("company_name")).lower()
    basis = email or li or (f"{name}|{dom}" if name and dom else "")
    if not basis:
        return ""
    return hashlib.sha256(basis.encode()).hexdigest()[:32]


def _walk_linkedins(obj: Any, out: list[tuple[str, dict[str, Any]]], ctx: dict[str, Any] | None = None) -> None:
    """Collect every linkedin.com/in URL in a vendor payload with its nearest
    dict (so we can read name / title / location next to it)."""
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, str):
                u = norm_linkedin(v)
                if u:
                    out.append((u, obj))
            else:
                _walk_linkedins(v, out, obj)
    elif isinstance(obj, list):
        for v in obj:
            _walk_linkedins(v, out, ctx)


def _ratio(a: str, b: str) -> float:
    a, b = _clean(a).lower(), _clean(b).lower()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _pick(d: dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for kk in ("name", "full", "value", "title"):
                if isinstance(v.get(kk), str) and v[kk].strip():
                    return v[kk].strip()
    return ""


def score_linkedin(target: dict[str, Any], cand: dict[str, Any]) -> tuple[float, dict[str, float]]:
    """0..1 score. Name 45%, company/domain 30%, title 15%, location 10%.
    Missing candidate fields are neutral (excluded from the weight)."""
    want_name = _clean(target.get("full_name") or f"{target.get('first_name','')} {target.get('last_name','')}")
    parts: dict[str, tuple[float, float]] = {}
    cname = cand.get("name") or ""
    if want_name and cname:
        s = _ratio(want_name, cname)
        wl = _clean(target.get("last_name") or want_name.split()[-1]).lower()
        if wl and wl not in cname.lower():
            s = min(s, 0.4)
        parts["name"] = (s, 0.45)
    ccomp = cand.get("company") or ""
    cdom = (cand.get("domain") or "").lower()
    tdom = _clean(target.get("domain")).lower()
    tcomp = _clean(target.get("company_name"))
    if (ccomp or cdom) and (tdom or tcomp):
        s = 0.0
        if tdom and cdom and (tdom == cdom or tdom.endswith("." + cdom) or cdom.endswith("." + tdom)):
            s = 1.0
        elif tcomp and ccomp:
            s = _ratio(tcomp, ccomp)
            if tcomp.lower() in ccomp.lower() or ccomp.lower() in tcomp.lower():
                s = max(s, 0.9)
        elif tdom and ccomp:
            root = tdom.split(".")[0]
            s = 0.85 if root and root in re.sub(r"[^a-z0-9]", "", ccomp.lower()) else 0.2
        parts["company"] = (s, 0.30)
    if target.get("title") and cand.get("title"):
        parts["title"] = (_ratio(target["title"], cand["title"]), 0.15)
    if target.get("location") and cand.get("location"):
        tl, cl = _clean(target["location"]).lower(), _clean(cand["location"]).lower()
        s = 1.0 if (tl in cl or cl in tl) else _ratio(tl, cl)
        parts["location"] = (s, 0.10)
    if "name" not in parts:
        return 0.0, {k: v[0] for k, v in parts.items()}
    w = sum(x[1] for x in parts.values())
    total = sum(x[0] * x[1] for x in parts.values()) / w if w else 0.0
    # A wrong name can never be rescued by company/title.
    if parts["name"][0] < 0.6:
        total = min(total, 0.5)
    # A name-only match is never enough on its own.
    if "company" not in parts:
        total = min(total, 0.6)
    return round(total, 3), {k: round(v[0], 3) for k, v in parts.items()}


def _cand_from_ctx(url: str, ctx: dict[str, Any], source: str) -> dict[str, Any]:
    prof = ctx.get("profile") if isinstance(ctx.get("profile"), dict) else ctx
    name = _pick(prof, "full_name", "name", "fullName") or _clean(
        f"{_pick(prof,'first_name','firstname')} {_pick(prof,'last_name','lastname')}")
    company = ""
    domain = ""
    for src in (prof, ctx.get("company") if isinstance(ctx.get("company"), dict) else {},
                ctx.get("experience") if isinstance(ctx.get("experience"), dict) else {}):
        company = company or _pick(src, "company_name", "company", "organization", "current_company")
        domain = domain or _pick(src, "company_domain", "domain", "company_website", "website")
    domain = re.sub(r"^https?://(www\.)?", "", domain.lower()).split("/")[0]
    return {
        "url": url,
        "source": source,
        "name": name,
        "title": _pick(prof, "title", "job_title", "headline", "position"),
        "company": company,
        "domain": domain,
        "location": _pick(prof, "location", "city", "region", "country"),
    }


# ---------------------------------------------------------------- cache

def cache_get(key: str) -> dict[str, Any] | None:
    if not key:
        return None
    try:
        from email_waterfall import supabase_sync as sb
        from email_waterfall.config import load_settings
        if not load_settings().supabase_configured:
            return None
        _s, text = sb._request(
            "GET", f"{CACHE_TABLE}?person_key=eq.{key}&select=result,updated_at&limit=1",
            prefer="return=representation")
        rows = json.loads(text or "[]")
        if not rows:
            return None
        res = rows[0].get("result") or {}
        ts = res.get("_cached_at") or 0
        if ts and time.time() - float(ts) > CACHE_TTL_DAYS * 86400:
            return None
        return res
    except Exception:
        return None


def cache_put(key: str, client_tag: str, result: dict[str, Any]) -> bool:
    if not key:
        return False
    try:
        from email_waterfall import supabase_sync as sb
        from email_waterfall.config import load_settings
        if not load_settings().supabase_configured:
            return False
        body = {"person_key": key, "client_tag": client_tag,
                "result": {**result, "_cached_at": time.time()}}
        sb._request("POST", f"{CACHE_TABLE}?on_conflict=person_key", body=[body],
                    prefer="resolution=merge-duplicates,return=minimal")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- verdicts

def fetch_verdicts(payload: dict[str, Any], *, timeout: int = 60) -> dict[str, Any] | None:
    base = (os.environ.get("REPLYHANDLER_URL") or "").rstrip("/")
    token = os.environ.get("LEAD_VERDICT_TOKEN") or ""
    if not base or not token:
        return None
    req = urlrequest.Request(
        f"{base}/internal/lead-verdict",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except (urlerror.URLError, ValueError, TimeoutError):
        return None


def dm_floor(title: str) -> dict[str, str] | None:
    """Deterministic floor applied before / over the AI: sales reps and
    coordinators are never decision makers."""
    t = _clean(title)
    if t and _DM_NO.search(t) and not _DM_EXEC.search(t):
        return {"verdict": "no", "reason": f"not a buying role ({t})"}
    return None


# ---------------------------------------------------------------- main

def enrich_call_ready(
    *,
    client_tag: str,
    first_name: str = "",
    last_name: str = "",
    full_name: str = "",
    email: str = "",
    company_name: str = "",
    domain: str = "",
    title: str = "",
    location: str = "",
    linkedin_url: str = "",
    known_phones: list[dict[str, str] | str] | None = None,
    approve_cost_usd: float | None = HARD_CAP_USD,
    steps: str | list[str] | None = None,
    use_cache: bool = True,
    vendors: dict[str, Any] | None = None,
    verdict_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    waterfall_phone_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not _clean(client_tag):
        raise ValueError("client_tag is required")
    if isinstance(steps, str):
        steps = [s.strip() for s in steps.split(",") if s.strip()]
    steps_set = set(steps or ["phone", "linkedin", "icp", "dm"])
    cap = HARD_CAP_USD if approve_cost_usd is None else float(approve_cost_usd)
    if cap < 0:
        raise ValueError("approve_cost_usd must be >= 0")
    budget = Budget(cap)

    if full_name and not (first_name or last_name):
        bits = full_name.split()
        first_name, last_name = (bits[0], bits[-1]) if len(bits) > 1 else (full_name, "")
    full_name = _clean(full_name or f"{first_name} {last_name}")
    domain = re.sub(r"^https?://(www\.)?", "", _clean(domain).lower()).split("/")[0]
    if not domain and "@" in email:
        d = email.split("@", 1)[1].lower()
        if d not in {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com"}:
            domain = d
    target = {"first_name": first_name, "last_name": last_name, "full_name": full_name,
              "email": email, "company_name": company_name, "domain": domain,
              "title": title, "location": location, "linkedin_url": linkedin_url}
    key = person_key(target)

    if use_cache:
        cached = cache_get(key)
        if cached and all(cached.get(f"_{s}_done") for s in steps_set):
            out = {k: v for k, v in cached.items() if not k.startswith("_")}
            out.update({"cached": True, "cost_usd": 0.0, "cached_cost_usd": cached.get("cost_usd", 0.0)})
            return out

    v = vendors or {}
    if not vendors:
        from email_waterfall.vendors.ai_ark import AiArkClient
        from email_waterfall.vendors.fullenrich import FullEnrichClient
        from email_waterfall.vendors.prospeo import ProspeoClient
        from email_waterfall.vendors.veriphone import VeriphoneClient
        v = {"fullenrich": FullEnrichClient(), "veriphone": VeriphoneClient(),
             "aiark": AiArkClient(), "prospeo": ProspeoClient()}

    receipt: list[dict[str, Any]] = []
    li_payloads: list[tuple[str, Any]] = []
    out: dict[str, Any] = {
        "ok": True, "client_tag": client_tag, "cached": False,
        "phone": "", "phone_type": "", "phone_verified": False, "phone_source": "",
        "linkedin_url": "", "linkedin_confidence": "", "linkedin_score": 0.0,
        "icp_verdict": "", "icp_reason": "", "dm_verdict": "", "dm_reason": "",
        "cost_usd": 0.0, "approve_cost_usd": budget.cap,
    }

    # ---- 1. phone
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(phone: str, source: str) -> None:
        n = norm_phone(phone)
        if len(n) >= 10 and n not in seen:
            seen.add(n)
            candidates.append({"phone": phone, "source": source})

    verified: list[dict[str, Any]] = []

    def verify_pending() -> bool:
        """Verify unverified candidates; True once a valid mobile exists."""
        vp = v.get("veriphone")
        for c in candidates:
            if c.get("_checked"):
                continue
            c["_checked"] = True
            if not vp or not getattr(vp, "enabled", False):
                receipt.append({"step": "veriphone", "skipped": "unconfigured"})
                continue
            cost = veriphone_usd()
            if not budget.can(cost):
                budget.skipped.append("veriphone")
                continue
            res = vp.verify(c["phone"])
            budget.charge(cost)
            receipt.append({"step": "veriphone", "source": c["source"],
                            "valid": bool(res and res.phone_valid),
                            "type": (res.phone_type if res else "")})
            if res and res.phone_valid:
                verified.append({"phone": res.e164 or c["phone"], "type": res.phone_type or "unknown",
                                 "source": c["source"]})
        return any(x["type"] == "mobile" for x in verified)

    if "phone" in steps_set:
        fe = v.get("fullenrich")
        if fe and getattr(fe, "enabled", False):
            cost = fullenrich_phone_usd()
            if budget.can(cost):
                hit = fe.find_mobile(first_name, last_name, domain, company_name,
                                     linkedin_url=linkedin_url)
                # FullEnrich bills on hit; book worst case only when it hit.
                if hit:
                    budget.charge(cost)
                    add(hit.phone, "fullenrich")
                    li_payloads.append(("fullenrich", hit.raw))
                    for ph in (hit.raw.get("contact_info") or {}).get("phones") or []:
                        val = ph.get("number") if isinstance(ph, dict) else ph
                        if val:
                            add(str(val), "fullenrich")
                receipt.append({"step": "fullenrich_phone", "hit": bool(hit),
                                "usd": cost if hit else 0.0})
            else:
                budget.skipped.append("fullenrich_phone")
        have_mobile = verify_pending()
        if not have_mobile:
            for kp in known_phones or []:
                if isinstance(kp, dict):
                    add(str(kp.get("phone") or ""), str(kp.get("source") or "known"))
                else:
                    add(str(kp), "known")
            have_mobile = verify_pending()
        if not have_mobile and (domain or company_name or linkedin_url):
            remaining = round(budget.cap - budget.spent, 6)
            if remaining > 0:
                fn = waterfall_phone_fn
                if fn is None:
                    from email_waterfall._wf.one import enrich_one_person as fn  # type: ignore
                try:
                    hit = fn(client_tag=client_tag, email=email, first_name=first_name,
                             last_name=last_name, full_name=full_name, linkedin_url=linkedin_url,
                             company_name=company_name, domain=domain, need="phone",
                             max_tier="prospeo", write_supabase=False,
                             approve_cost_usd=remaining)
                except Exception as exc:  # noqa: BLE001
                    hit = {"ok": False, "reason": type(exc).__name__}
                sp = hit.get("spend")
                spent = float((sp.get("total_usd") if isinstance(sp, dict) else sp) or 0.0)
                budget.charge(spent)
                receipt.append({"step": "waterfall_phone", "tier": hit.get("phone_tier"), "usd": spent})
                if hit.get("phone"):
                    add(hit["phone"], f"waterfall:{hit.get('phone_tier') or 'unknown'}")
                if hit.get("linkedin_url") and not linkedin_url:
                    li_payloads.append(("waterfall", {"linkedin_url": hit["linkedin_url"],
                                                      "name": full_name, "domain": domain}))
                verify_pending()
        if verified:
            verified.sort(key=lambda x: PHONE_TYPE_RANK.get(x["type"], 5))
            best = verified[0]
            out.update(phone=best["phone"], phone_type=best["type"], phone_verified=True,
                       phone_source=best["source"])
        elif candidates:
            out.update(phone=candidates[0]["phone"], phone_type="unverified",
                       phone_verified=False, phone_source=candidates[0]["source"])

    # ---- 2. linkedin
    if "linkedin" in steps_set:
        cands: dict[str, dict[str, Any]] = {}
        if linkedin_url and norm_linkedin(linkedin_url):
            u = norm_linkedin(linkedin_url)
            cands[u] = {"url": u, "source": "input", "name": full_name, "company": company_name,
                        "domain": domain, "title": title, "location": location}
        ai = v.get("aiark")
        if ai and getattr(ai, "enabled", False) and (domain or company_name) and full_name:
            cost = round(6.5 * USD_PER_CREDIT["aiark"], 6)
            if budget.can(cost):
                people = ai.find_people(domain, company_name=company_name, full_name=full_name, limit=5)
                if people:
                    budget.charge(cost)
                for p in people:
                    u = norm_linkedin(p.linkedin_url)
                    if u and u not in cands:
                        cands[u] = {"url": u, "source": "aiark", "name": p.name, "title": p.title,
                                    "company": company_name if domain else "", "domain": domain,
                                    "location": _pick(p.raw.get("profile") or {}, "location") if isinstance(p.raw, dict) else ""}
                receipt.append({"step": "aiark_people", "n": len(people)})
        for src, payload in li_payloads:
            found: list[tuple[str, dict[str, Any]]] = []
            _walk_linkedins(payload, found)
            for u, ctx in found:
                if u not in cands:
                    c = _cand_from_ctx(u, ctx, src)
                    if not c["name"]:
                        c["name"] = full_name  # vendor matched on our name input
                    if not (c["company"] or c["domain"]):
                        c["domain"] = domain
                    cands[u] = c
        pro = v.get("prospeo")
        if not cands and pro and getattr(pro, "enabled", False) and full_name and (domain or company_name):
            cost = round(1.0 * USD_PER_CREDIT["prospeo"], 6)
            if budget.can(cost):
                try:
                    hit = pro.find_email(first_name, last_name, domain, company_name)
                except Exception:  # noqa: BLE001
                    hit = None
                if hit:
                    budget.charge(cost)
                    found = []
                    _walk_linkedins(getattr(hit, "raw", {}) or {}, found)
                    for u, ctx in found:
                        c = _cand_from_ctx(u, ctx, "prospeo")
                        c["name"] = c["name"] or full_name
                        c["domain"] = c["domain"] or domain
                        cands.setdefault(u, c)
                receipt.append({"step": "prospeo_person", "hit": bool(hit)})
        scored = []
        for c in cands.values():
            s, parts = score_linkedin(target, c)
            if c["source"] == "input":
                s = max(s, 0.75)
            scored.append((s, parts, c))
        scored.sort(key=lambda x: -x[0])
        if scored:
            s, parts, c = scored[0]
            out.update(linkedin_url=c["url"], linkedin_score=s, linkedin_source=c["source"],
                       linkedin_confidence="high" if s >= 0.85 else ("medium" if s >= LINKEDIN_THRESHOLD else "review"),
                       linkedin_candidates=len(scored))
            if not title and c.get("title"):
                title = c["title"]
                target["title"] = title
        else:
            out.update(linkedin_confidence="review", linkedin_candidates=0)

    # ---- 3/4. ICP + decision maker
    if steps_set & {"icp", "dm"}:
        floor = dm_floor(title)
        cost = verdict_usd()
        res = None
        if budget.can(cost):
            payload = {"client_tag": client_tag, "domain": domain, "company_name": company_name,
                       "title": title, "full_name": full_name, "linkedin_url": out.get("linkedin_url") or ""}
            res = (verdict_fn or fetch_verdicts)(payload)
            if res:
                budget.charge(float(res.get("cost_usd") or cost))
        else:
            budget.skipped.append("verdicts")
        res = res or {}
        if "icp" in steps_set:
            out["icp_verdict"] = res.get("icp_verdict") or "unknown"
            out["icp_reason"] = res.get("icp_reason") or ("verdict service unavailable" if not res else "")
        if "dm" in steps_set:
            if floor:
                out["dm_verdict"], out["dm_reason"] = floor["verdict"], floor["reason"]
            else:
                out["dm_verdict"] = res.get("dm_verdict") or "unknown"
                out["dm_reason"] = res.get("dm_reason") or ("title unknown" if not title else "verdict service unavailable")
        out["title"] = title

    out["cost_usd"] = round(budget.spent, 4)
    out["skipped_for_cap"] = budget.skipped
    out["receipt"] = receipt
    if use_cache and key:
        prev = cache_get(key) or {}
        merged = {**prev, **{k: v2 for k, v2 in out.items() if k != "receipt"}}
        for s in steps_set:
            merged[f"_{s}_done"] = True
        cache_put(key, client_tag, merged)
    return out
