"""Stage assignment, evidence-strength scoring, and risk flags.

All deterministic and versioned (see rules.py). Every score is accompanied by
the rule breakdown so the UI can show *why* a company scored as it did.
No invented probabilities: evidence strength is a 0-100 documentation score.
"""
from datetime import datetime, timezone

from . import rules

# Dashboard groups (user-defined): five priority groups + completed archive +
# withdrawn/rejected outcome. Companies never advance on filing volume alone —
# only on new qualifying evidence.
STAGE_GROUPS = [
    {"key": "trading_date_announced", "label": "1. Trading date announced",
     "stages": ["approval_trading_date"],
     "rank": 1,
     "use": "Closest to completion; confirm conditions and subsequent updates."},
    {"key": "conditional_approval", "label": "2. Conditional approval",
     "stages": ["conditional_approval"],
     "rank": 2,
     "use": "Track each outstanding condition."},
    {"key": "application_submitted", "label": "3. Application submitted",
     "stages": ["application_submitted"],
     "rank": 3,
     "use": "Investigate financing, financial condition, and progress."},
    {"key": "preparation", "label": "4. Preparation underway",
     "stages": ["preparation"],
     "rank": 4,
     "use": "Early research watchlist."},
    {"key": "intent_stalled", "label": "5. Intent only / stalled",
     "stages": ["intent_announced", "insufficient_evidence"],
     "rank": 5,
     "use": "Lower priority; flag unresolved status."},
]
COMPLETED_STAGE = "trading_commenced"    # completed archive
WITHDRAWN_STAGE = "delayed_withdrawn"   # separate outcome

NEXT_MILESTONE = {
    "intent_announced": "Disclosure that a listing application was filed (8-K / press release).",
    "preparation": "Listing application submitted; reverse split implemented; financing closed.",
    "application_submitted": "Conditional approval, denial, or withdrawal disclosure.",
    "conditional_approval": "Satisfaction of each remaining condition; final approval.",
    "approval_trading_date": "First trading day confirmation (8-K / exchange notice).",
    "trading_commenced": "None — completed. Monitor for continued-listing compliance issues.",
    "delayed_withdrawn": "Re-filing of application or formal abandonment disclosure.",
    "insufficient_evidence": "Any substantive uplisting disclosure.",
}


def group_for_stage(stage):
    for g in STAGE_GROUPS:
        if stage in g["stages"]:
            return g
    if stage == COMPLETED_STAGE:
        return {"key": "completed", "label": "Completed — trading commenced",
                "stages": [COMPLETED_STAGE], "rank": 0,
                "use": "Historical archive; not active candidates."}
    if stage == WITHDRAWN_STAGE:
        return {"key": "withdrawn", "label": "Withdrawn / rejected",
                "stages": [WITHDRAWN_STAGE], "rank": 9,
                "use": "Separate outcome; not active candidates."}
    return {"key": "other", "label": stage, "stages": [stage], "rank": 6, "use": ""}


# evidence_type -> stage it supports (strongest wins by STAGE_ORDER)
EVIDENCE_TO_STAGE = {
    "trading_commencement": "trading_commenced",
    "approval_notice": "approval_trading_date",
    "conditional_approval": "conditional_approval",
    "application_filed": "application_submitted",
    "preparation_activity": "preparation",
    "reverse_split": "preparation",
    "registration_8a": "preparation",
    "intent_statement": "intent_announced",
    "negated_or_withdrawn": "delayed_withdrawn",
    "delay_withdrawal": "delayed_withdrawn",
}


def assign_stage(evidence_types, has_conflict=False):
    """evidence_types: iterable of evidence_type strings (non-excluded only)."""
    types = set(evidence_types)
    if has_conflict:
        return "insufficient_evidence", "conflicting statements across filings"
    # Withdrawal/delay overrides forward progress only if it is the LATEST signal;
    # the pipeline passes ordered evidence, so check recency there. Here: if the
    # only substantive evidence is negated/withdrawn -> delayed_withdrawn.
    substantive = {t for t in types if t in EVIDENCE_TO_STAGE}
    if not substantive:
        return "insufficient_evidence", "no substantive uplisting evidence"
    if substantive == {"negated_or_withdrawn"} or substantive == {"delay_withdrawal"}:
        return "delayed_withdrawn", "withdrawal/delay disclosed"
    # Highest stage wins
    best = None
    for stage in rules.STAGE_ORDER:
        for ev, st in EVIDENCE_TO_STAGE.items():
            if st == stage and ev in substantive:
                best = stage
    if best:
        req = rules.STAGES[best]["requires"]
        return best, f"supported by evidence: {', '.join(sorted(substantive & set(_stage_evidence(best)))) or ', '.join(sorted(substantive))}"
    return "insufficient_evidence", "evidence did not map to a stage"


def _stage_evidence(stage):
    out = []
    for ev, st in EVIDENCE_TO_STAGE.items():
        if st == stage:
            out.append(ev)
    return out


def score_evidence(company_evidence, filings_meta=None, now=None):
    """Return (score 0..100, band, breakdown list of {rule, points, note})."""
    now = now or datetime.now(timezone.utc)
    breakdown = []
    total = 0

    def add(rule_id, ok, note=""):
        nonlocal total
        pts = next(p for rid, p, _d in rules.SCORING_RULES if rid == rule_id)
        desc = next(d for rid, p, d in rules.SCORING_RULES if rid == rule_id)
        if ok:
            total += pts
            breakdown.append({"rule": rule_id, "points": pts, "note": note or desc})
        return ok

    types = {e["evidence_type"] for e in company_evidence}
    exchanges = {e.get("exchange_mentioned") for e in company_evidence if e.get("exchange_mentioned")}

    add("issuer_intent", "intent_statement" in types)
    add("application_disclosed", "application_filed" in types)
    add("exchange_named", bool(exchanges - {""}))
    add("conditional_approval", "conditional_approval" in types)
    add("approval_with_date",
        "approval_notice" in types and any(e.get("event_date") for e in company_evidence
                                           if e["evidence_type"] == "approval_notice"))
    add("trading_confirmed", "trading_commencement" in types)
    add("exchange_notice", any(e.get("independent_exchange_notice") for e in company_evidence))
    add("form_8a_common", any(e["evidence_type"] == "registration_8a"
                              and e.get("security_type") == "common_stock"
                              for e in company_evidence))
    add("effect_linked", any(e["evidence_type"] == "effect_linked" for e in company_evidence))

    # recency
    dates = [e.get("filing_date") for e in company_evidence if e.get("filing_date")]
    if dates:
        latest = max(dates)
        try:
            dt = datetime.fromisoformat(latest)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_days = (now - dt).days
            add("recent_update", age_days <= 90, f"latest evidence {latest}")
            add("stale", age_days > 180, f"latest evidence {latest} ({age_days}d ago)")
        except Exception:
            pass

    add("boilerplate_only", not types and any(
        e.get("exclusion_reason") == "boilerplate" for e in company_evidence),
        "only boilerplate references")
    add("conflicting", any(e.get("conflict") for e in company_evidence),
        "contradictory statements")
    add("continued_compliance_ctx", any(
        e.get("exclusion_reason") == "continued_listing_compliance" for e in company_evidence),
        "continued-listing compliance context")

    total = max(0, min(100, total))
    return total, rules.score_band(total), breakdown


def detect_risk_flags(company, evidence, filings_meta, market):
    """Return list of {flag, severity, evidence, detail}. Only supported flags."""
    flags = []

    def add(flag_id, detail, evidence_ref=""):
        sev = next(s for fid, s, _d, _det in rules.RISK_FLAG_DEFS if fid == flag_id)
        flags.append({"flag": flag_id, "severity": sev, "detail": detail,
                      "evidence": evidence_ref})

    # Going concern
    for e in evidence:
        if "going concern" in (e.get("excerpt") or "").lower():
            add("going_concern", "Going-concern language in filed document",
                e.get("filing_accession", ""))
            break
    # Reverse split
    splits = [m.get("split_ratio") for m in (filings_meta or []) if m.get("split_ratio")]
    if splits:
        add("reverse_split", f"Reverse split disclosed: {splits[0]}", "filing text")
    # Offering / dilution
    offerings = [m.get("offering") for m in (filings_meta or []) if m.get("offering")]
    shares_out = (market or {}).get("shares_outstanding")
    for off in offerings:
        try:
            sh = float(off["shares"])
            if shares_out:
                pct = sh / float(shares_out) * 100
                add("dilution_offering",
                    f"Offering of {sh:,.0f} shares ≈ {pct:.1f}% of {float(shares_out):,.0f} "
                    f"outstanding (assumes outstanding count current)",
                    "S-1/424B prospectus")
            else:
                add("dilution_offering",
                    f"Offering of {sh:,.0f} shares; shares outstanding unknown — "
                    f"dilution % cannot be computed",
                    "S-1/424B prospectus")
        except (ValueError, TypeError, KeyError):
            pass
        break  # one flag per company for the latest offering
    # Liquidity unknown
    if not (market or {}).get("price"):
        add("liquidity_unknown", "No reliable quote/volume data from configured providers",
            "market data")
    # Stale evidence
    dates = [e.get("filing_date") for e in evidence if e.get("filing_date")]
    if dates:
        latest = max(dates)
        try:
            dt = datetime.fromisoformat(latest)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - dt).days > 180:
                add("stale_evidence", f"No new uplisting evidence since {latest}", "")
        except Exception:
            pass
    return flags


# ---------------------------------------------------------------------------
# "What remains?" checklist. Each item: confirmed / outstanding / unknown /
# none_found, with the supporting source. Unknown stays unknown — the portal
# never infers that listing requirements are satisfied from incomplete info.
# ---------------------------------------------------------------------------
import re as _re


def _src(e):
    if not e:
        return None
    return {"excerpt": e.get("excerpt", ""),
            "accession": e.get("filing_accession") or e.get("accession", ""),
            "form": e.get("form", ""), "filing_date": e.get("filing_date", ""),
            "url": e.get("url", "")}


def _first(evidence, types):
    for e in evidence:
        if e.get("evidence_type") in types:
            return e
    return None


def _extract_conditions(excerpt):
    """Pull 'subject to / provided that / including' condition clauses."""
    conds = []
    for m in _re.finditer(
            r"(?:subject\s+to|provided\s+that|conditioned\s+(?:up)?on|including\s+the\s+satisfaction\s+of)\s+([^.;]{10,220})",
            excerpt or "", _re.I):
        c = _re.sub(r"\s+", " ", m.group(1)).strip()
        if c and c not in conds:
            conds.append(c)
    return conds


def _split_status(evidence):
    """Classify reverse-split disclosures: implemented / authorized / required."""
    texts = " ".join((e.get("excerpt") or "") for e in evidence
                     if e.get("evidence_type") == "reverse_split"
                     or "reverse" in (e.get("excerpt") or "").lower())
    if not texts.strip():
        return None, ""
    low = texts.lower()
    ratio_m = _re.search(r"(\d+\s*-\s*for\s*-\s*\d+)", texts, _re.I)
    ratio = ratio_m.group(1) if ratio_m else "ratio undisclosed"
    if _re.search(r"effected|has been effected|became effective|was completed|implemented", low):
        return "confirmed", f"implemented ({ratio})"
    if _re.search(r"stockholder.{0,40}approv|board.{0,40}approv|authoriz", low):
        return "outstanding", f"authorized/proposed ({ratio}); implementation not confirmed"
    return "outstanding", f"discussed as requirement ({ratio}); authorization/implementation unknown"


def build_checklist(evidence):
    """evidence: list of dicts with keys evidence_type, excerpt, filing_accession,
    form, filing_date, url, is_excluded. Returns (items, freshness_days)."""
    ev = [e for e in (evidence or []) if not e.get("is_excluded")]
    items = []

    def add(item, status, detail, source):
        items.append({"item": item, "status": status, "detail": detail,
                      "source": _src(source)})

    # 1. Application submitted?
    e = _first(ev, ("application_filed",))
    add("Exchange application submitted?",
        "confirmed" if e else "unknown",
        ("Listing application disclosed as filed " +
         (f"({e.get('filing_date')})" if e and e.get("filing_date") else "")) if e
        else "No filing found disclosing a submitted listing application.",
        e)

    # 2. Approval disclosed, and by whom?
    e = _first(ev, ("approval_notice", "trading_commencement"))
    ec = _first(ev, ("conditional_approval",))
    if e or ec:
        use = e or ec
        by_whom = "exchange" if use.get("independent_exchange_notice") else "issuer"
        kind = "final approval" if e else "conditional approval"
        add("Approval disclosed, and by whom?", "confirmed",
            f"{kind.title()} disclosed by {by_whom} ({use.get('filing_date', '?')})." +
            ("" if by_whom == "exchange"
             else " Issuer claim — not independently confirmed by the exchange."),
            use)
    else:
        add("Approval disclosed, and by whom?", "unknown",
            "No approval disclosure found.", None)

    # 3. Remaining approval conditions?
    if ec:
        conds = []
        for x in ev:
            if x.get("evidence_type") == "conditional_approval":
                conds.extend(_extract_conditions(x.get("excerpt")))
        conds = conds or ["conditions referenced but not itemized in extracted text"]
        add("Remaining approval conditions?", "outstanding",
            "Conditions disclosed: " + "; ".join(conds[:4]), ec)
    elif e:
        add("Remaining approval conditions?", "none_found",
            "Approval disclosed without referenced remaining conditions.", e)
    else:
        add("Remaining approval conditions?", "unknown",
            "No approval disclosure to evaluate.", None)

    # 4. Financing required, and has it closed?
    fin = _first(ev, ("financing_terms",))
    if fin:
        ex = (fin.get("excerpt") or "").lower()
        closed = bool(_re.search(r"\bclosed\b|\bcompleted\b|concurrent closing|pricing of", ex)) \
            or fin.get("form") in ("424B4", "424B5")
        add("Financing required, and has it closed?",
            "confirmed" if closed else "outstanding",
            (fin.get("context") or "Offering disclosed") +
            (" — closing confirmed." if closed else " — closing not confirmed in filings reviewed."),
            fin)
    else:
        add("Financing required, and has it closed?", "unknown",
            "No financing terms disclosed in reviewed filings. A direct uplisting "
            "may occur without a concurrent raise — unknown, not assumed.", None)

    # 5. Reverse split required, authorized, or implemented?
    st, detail = _split_status(ev)
    if st:
        add("Reverse split required, authorized, or implemented?", st, detail,
            _first(ev, ("reverse_split",)))
    else:
        add("Reverse split required, authorized, or implemented?", "unknown",
            "No reverse split disclosed in reviewed filings.", None)

    # 6. Common-stock exchange registration filed?
    r8 = _first(ev, ("registration_8a",))
    if r8 and r8.get("security_type") in ("common_stock", "mixed"):
        add("Common-stock exchange registration filed?", "confirmed",
            f"Form 8-A filed ({r8.get('filing_date', '?')}); registered security: "
            f"{r8.get('security_type')}.", r8)
    elif r8:
        add("Common-stock exchange registration filed?", "outstanding",
            f"Form 8-A filed but registers {r8.get('security_type')} — common-stock "
            f"registration not confirmed.", r8)
    else:
        add("Common-stock exchange registration filed?", "unknown",
            "No Form 8-A found in reviewed filings.", None)

    # 7. Specific trading date announced?
    td = None
    for x in ev:
        if x.get("event_date") and x.get("evidence_type") in (
                "approval_notice", "trading_commencement", "conditional_approval"):
            td = x
            break
    add("Specific trading date announced?",
        "confirmed" if td else "unknown",
        f"Expected/commenced trading date: {td['event_date']} "
        f"(disclosed {td.get('filing_date', '?')})." if td
        else "No specific trading date disclosed.", td)

    # 8. Later withdrawal, delay, or contradiction?
    wd = _first(ev, ("delay_withdrawal", "negated_or_withdrawn"))
    add("Later withdrawal, delay, or contradictory disclosure?",
        "confirmed" if wd else "none_found",
        ("Disclosure found: " + (wd.get("excerpt") or "")[:220]) if wd
        else "None disclosed in filings reviewed.", wd)

    # freshness
    dates = [x.get("filing_date") for x in ev if x.get("filing_date")]
    freshness = None
    if dates:
        try:
            latest = max(dates)
            dt = datetime.fromisoformat(latest)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            freshness = (datetime.now(timezone.utc) - dt).days
        except Exception:
            pass
    return items, freshness


def outstanding_conditions_count(evidence):
    """Count of checklist items that are outstanding (for ranking)."""
    items, _ = build_checklist(evidence)
    return sum(1 for i in items if i["status"] == "outstanding")
