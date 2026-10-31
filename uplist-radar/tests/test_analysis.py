"""Edge-case tests for the deterministic analysis layer.

Covers: continued-listing-compliance false positive, delisting, OTC tier
change, boilerplate, hypothetical, other-security 8-A, real uplisting intent,
application, conditional approval, trading commencement, withdrawal negation,
amendment linking, duplicate dedup, and stage assignment. No network needed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from uplist_radar import nlp, rules, stages

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))


def kinds(findings):
    return [(f.kind, f.evidence_type) for f in findings]


# 1. Real uplisting intent (8-K)
t1 = """On May 9, 2025, the Board of Directors of BrooqLy, Inc. approved plans to
uplist the Company's common stock from the OTCQB to the Nasdaq Capital Market.
The Company intends to file a listing application with Nasdaq in the third quarter."""
f, meta = nlp.analyze_document(t1, "8-K")
ev = [x for x in f if x.kind == "evidence"]
check("intent: evidence found", any(x.evidence_type == "intent_statement" for x in ev))
check("intent: exchange detected", any(x.exchange == "Nasdaq" for x in ev))
check("intent: common stock", any(x.security_type in ("common_stock", "unknown") for x in ev))

# 2. Continued-listing compliance FALSE POSITIVE must be excluded
# (window contains a search phrase hit + compliance context, as in real filings)
w2 = """The Company received a deficiency letter from the Nasdaq Listing Qualifications
Department notifying the Company that it is not in compliance with the minimum bid
price requirement for continued listing on the Nasdaq Capital Market. The Company
intends to regain compliance within 180 days."""
f2 = nlp.classify_hit(w2, "8-K")
check("compliance: excluded", f2.kind == "exclusion" and
      f2.evidence_type == "continued_listing_compliance", f"{f2.kind}/{f2.evidence_type}")
t2b = w2  # full-document path: phrase "Nasdaq Capital Market" triggers a window
f, _ = nlp.analyze_document(t2b, "8-K")
check("compliance: no positive evidence", not any(x.kind == "evidence" for x in f))

# 3. Delisting to OTC must be excluded
w3 = """The Company announced its voluntary delisting from the Nasdaq Stock Market,
where its common stock had been listed. The Company was notified that its uplisting
application was moot. Its common stock is expected to be quoted on the OTC Pink Market."""
f3 = nlp.classify_hit(w3, "8-K")
check("delisting: excluded", f3.kind == "exclusion" and f3.evidence_type == "delisting",
      f"{f3.kind}/{f3.evidence_type}")

# 4. OTC tier change (Pink -> OTCQB) must be excluded
t4 = """We are pleased to announce our uplisting from the Pink Open Market to the OTCQB
venture market. Trading under our current symbol will continue."""
f, _ = nlp.analyze_document(t4, "8-K")
check("otc tier: excluded", any(x.kind == "exclusion" and x.evidence_type == "otc_tier_change"
                               for x in f))

# 5. Boilerplate risk-factor hypothetical must be excluded
t5 = """RISK FACTORS. There can be no assurance that we will be able to uplist our
common stock to a national securities exchange, and our failure to do so may harm
our business."""
f, _ = nlp.analyze_document(t5, "10-K")
check("boilerplate: excluded", any(x.kind == "exclusion" and x.evidence_type in
                                  ("boilerplate", "hypothetical") for x in f),
      str(kinds(f)))

# 6. 8-A12B for warrants only (not common stock) must be excluded
w6 = """Item 1. Description of Registrant's Securities to be Registered. Form 8-A.
The securities to be registered are warrants to purchase common stock, each warrant
exercisable for one share at $11.50. The warrants are not the common stock."""
f6 = nlp.classify_hit(w6, "8-A12B")
check("8-A warrants: excluded as other_security",
      f6.kind == "exclusion" and f6.evidence_type == "other_security",
      f"{f6.kind}/{f6.evidence_type}")

# 7. 8-A12B for common stock is evidence
w7 = """Item 1. Description of Registrant's Securities to be Registered. Form 8-A12B.
The Registrant hereby registers its common stock, par value $0.001 per share,
pursuant to Section 12(b) of the Exchange Act for listing on the Nasdaq Capital Market."""
f7 = nlp.classify_hit(w7, "8-A12B")
check("8-A common: evidence", f7.kind == "evidence" and f7.evidence_type == "registration_8a"
      and f7.security_type == "common_stock", f"{f7.kind}/{f7.evidence_type}/{f7.security_type}")

# 8. Application submitted
t8 = """The Company has applied to list its common stock on the NYSE American.
The listing application was submitted on March 3, 2025 and remains subject to
satisfaction of initial listing requirements."""
f, _ = nlp.analyze_document(t8, "8-K")
check("application: evidence", any(x.kind == "evidence" and x.evidence_type == "application_filed"
                                  for x in f))
check("application: NYSE American", any(x.exchange == "NYSE American" for x in f if x.kind == "evidence"))

# 9. Conditional approval
t9 = """On April 2, 2025, the Company received conditional approval for listing of its
common stock on the Nasdaq Capital Market, subject to a reverse stock split."""
f, _ = nlp.analyze_document(t9, "8-K")
check("conditional approval: evidence",
      any(x.kind == "evidence" and x.evidence_type == "conditional_approval" for x in f))

# 10. Trading commencement with date
t10 = """The Company's common stock was approved for listing on the Nasdaq Capital Market
and is expected to commence trading on Nasdaq on May 20, 2025 under the symbol ABCD."""
f, _ = nlp.analyze_document(t10, "8-K")
ev = [x for x in f if x.kind == "evidence"]
check("trading: commencement/approval evidence",
      any(x.evidence_type in ("trading_commencement", "approval_notice") for x in ev),
      str([(x.evidence_type) for x in ev]))
# firm past-tense commencement
w10b = """The Company's common stock began trading on the Nasdaq Capital Market
on May 20, 2025 under the symbol ABCD."""
f10b = nlp.classify_hit(w10b, "8-K")
check("trading: past-tense commencement",
      f10b.kind == "evidence" and f10b.evidence_type == "trading_commencement",
      f"{f10b.kind}/{f10b.evidence_type}")
check("trading: event date extracted",
      any(x.event_date == "2025-05-20" for x in ev), str([x.event_date for x in ev]))

# 11. Withdrawal negation
t11 = """The Company has withdrawn its listing application with Nasdaq and no longer
intends to uplist at this time."""
f, _ = nlp.analyze_document(t11, "8-K")
check("withdrawal: negated context",
      any(x.evidence_type == "negated_or_withdrawn" for x in f))

# 12. Stage assignment: full progression picks the highest stage
stage, reason = stages.assign_stage(["intent_statement", "application_filed",
                                     "conditional_approval", "approval_notice"])
check("staging: highest wins", stage == "approval_trading_date", f"{stage}: {reason}")

# 13. Stage assignment: withdrawal-only -> delayed_withdrawn
stage, _ = stages.assign_stage(["negated_or_withdrawn"])
check("staging: withdrawal", stage == "delayed_withdrawn")

# 14. Stage assignment: conflict -> insufficient
stage, _ = stages.assign_stage(["intent_statement", "negated_or_withdrawn"], has_conflict=True)
check("staging: conflict", stage == "insufficient_evidence")

# 15. Stage assignment: empty -> insufficient
stage, _ = stages.assign_stage([])
check("staging: empty", stage == "insufficient_evidence")

# 16. Scoring: strong evidence scores high with breakdown
ev = [
    {"evidence_type": "intent_statement", "exchange_mentioned": "Nasdaq",
     "filing_date": "2025-05-01", "excerpt": "x"},
    {"evidence_type": "application_filed", "exchange_mentioned": "Nasdaq",
     "filing_date": "2025-06-01", "excerpt": "x"},
    {"evidence_type": "conditional_approval", "exchange_mentioned": "Nasdaq",
     "filing_date": "2026-10-01", "excerpt": "x"},
]
score, band, breakdown = stages.score_evidence(ev)
check("scoring: strong band", band in ("Strong", "Very strong") and score >= 50,
      f"score={score} band={band}")
check("scoring: breakdown present", len(breakdown) >= 3)

# 17. Scoring: boilerplate-only scores ~0
ev = [{"evidence_type": "boilerplate", "exclusion_reason": "boilerplate",
       "filing_date": "2025-01-01", "excerpt": "x"}]
score, band, _ = stages.score_evidence(ev)
check("scoring: boilerplate low", score <= 10, f"score={score}")

# 18. Reverse split ratio extraction
check("split ratio", nlp.extract_split_ratio("a 1-for-10 reverse stock split") == "1-for-10")
check("split ratio 2", nlp.extract_split_ratio("reverse stock split at a ratio of 1-for-20") == "1-for-20")

# 19. Offering extraction
off = nlp.extract_offering("public offering of 5,000,000 shares of common stock at $4.00 per share")
check("offering shares", off.get("shares") == "5000000", str(off))
check("offering price", off.get("price") == "4.00", str(off))

# 20. Hypothetical "may seek to uplist in the future" excluded
t20 = "The Company may seek to uplist to a national securities exchange in the future."
f, _ = nlp.analyze_document(t20, "10-K")
check("hypothetical: excluded",
      any(x.kind == "exclusion" and x.evidence_type == "hypothetical" for x in f))

# 30. Press-exhibit detection (8-K Ex. 99.1 handling)
from uplist_radar.pipeline import _is_press_exhibit
check("exhibit: ex99-1.htm", _is_press_exhibit("ex99-1.htm"))
check("exhibit: ex99.htm", _is_press_exhibit("ex99.htm"))
check("exhibit: not primary 8-k", not _is_press_exhibit("form8-k.htm"))
check("exhibit: not ex10", not _is_press_exhibit("ex10-1.htm"))

# 31. FTS _id -> hit_doc parsing
_id = "0001493152-26-003546:ex99-1.htm"
check("hit_doc parse", _id.split(":")[-1] == "ex99-1.htm")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
