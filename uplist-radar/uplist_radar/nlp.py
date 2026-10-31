"""Deterministic contextual analysis of filing text.

Treats filing content as untrusted input: no instructions in documents are
followed; only evidence is extracted. Keyword hits are never enough on their
own — every hit is examined with surrounding context, section awareness,
negation/hypothetical detection, and exchange/security disambiguation.

Pipeline per document:
  1. find keyword/phrase hits with context windows
  2. classify each hit -> evidence_type or exclusion (+reason)
  3. aggregate per filing -> findings list with excerpts
"""
import re
from dataclasses import dataclass, field

from . import rules

WINDOW_CHARS = 600  # context window around a hit

EXCHANGE_PATTERNS = [
    (r"Nasdaq\s+Capital\s+Market", "Nasdaq"),
    (r"Nasdaq\s+Global\s+(?:Select\s+|)\s*Market", "Nasdaq"),
    (r"(?:the\s+)?Nasdaq\s+(?:Stock\s+Market)?", "Nasdaq"),
    (r"NYSE\s+American", "NYSE American"),
    (r"(?:the\s+)?New\s+York\s+Stock\s+Exchange", "NYSE"),
    (r"(?<!\w)NYSE(?!\s+American)(?!\w)", "NYSE"),
]

RISK_FACTOR_HEADS = re.compile(r"risk\s+factors?", re.I)
SECTION_HEAD = re.compile(r"^\s*(item\s+\d+[A-Z]?\b.{0,80})$", re.I | re.M)

HYPOTHETICAL_PATTERNS = [
    r"\bmay\s+(?:seek\s+to\s+|consider\s+|decide\s+to\s+)?uplist",
    r"\bno\s+(?:current\s+)?plans?\s+to\s+uplist",
    r"\bif\s+we\s+(?:are\s+able\s+to\s+|were\s+to\s+)?uplist",
    r"\bcould\s+uplist",
    r"\bhypothetical",
    r"there\s+can\s+be\s+no\s+assurance",
    r"\bintends?\s+to\s+evaluate\b.{0,40}\buplist",
]

NEGATION_PATTERNS = [
    r"\b(not|never|no\s+longer|failed\s+to|unable\s+to)\b.{0,60}uplist",
    r"\bwithdraw\w*\b.{0,60}\b(listing\s+application|uplist)",
    r"\b(listing\s+application|uplist\w*)\b.{0,60}\bwithdrawn\b",
    r"\bterminat\w*\b.{0,60}\blisting\s+application",
    r"\breject\w*\b.{0,60}\blisting\s+application",
]

CONTINUED_COMPLIANCE_PATTERNS = [
    r"deficiency\s+(letter|notice)",
    r"Nasdaq\s+Listing\s+Qualifications?",
    r"minimum\s+bid\s+price",
    r"regain\s+compliance",
    r"Listing\s+Rule\s+5\d{3}",
    r"delisting\s+determination",
    r"request(?:ed|ing)?\s+a\s+hearing",
]

DELISTING_PATTERNS = [
    r"voluntary\s+delisting",
    r"delist\w*\s+(?:its\s+common\s+stock\s+)?from\s+(?:the\s+)?(?:Nasdaq|NYSE)",
    r"transfer\s+.*\bto\s+the\s+OTC\b",
    r"quoted\s+on\s+the\s+OTC.*following\s+delisting",
]

OTC_TIER_PATTERNS = [
    r"uplist\w*\s+(?:from\s+(?:the\s+)?Pink\s+(?:Open\s+Market\s+|Market\s+)?)?to\s+(?:the\s+)?OTCQB",
    r"uplist\w*\s+to\s+(?:the\s+)?OTCQX",
    r"approved\s+for\s+quotation\s+on\s+(?:the\s+)?OTCQB",
]

OTHER_SECURITY_PATTERNS = [
    r"warrants?\s+to\s+purchase",
    r"preferred\s+stock",
    r"series\s+[A-Z]\s+preferred",
    r"convertible\s+(?:promissory\s+)?notes?",
    r"debt\s+securities",
]

SPLIT_RATIO_RE = re.compile(r"(\d+)\s*-\s*for\s*-\s*(\d+)\s+reverse\s+stock\s+split", re.I)
SPLIT_RATIO_RE2 = re.compile(r"reverse\s+stock\s+split\s+(?:at\s+a\s+ratio\s+of\s+)?(\d+)\s*[:\-]?\s*for\s*[:\-]?\s*(\d+)", re.I)
OFFERING_RE = re.compile(
    r"offering\s+of\s+(?:up\s+to\s+)?([\d,]+)\s+shares?\s+of\s+(?:our\s+|the\s+)?common\s+stock"
    r"(?:\s+at\s+(?:a\s+)?(?:public\s+offering\s+price\s+of\s+)?\$?([\d.]+))?", re.I)
DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2}),\s+(\d{4})")


@dataclass
class Finding:
    kind: str                    # 'evidence' | 'exclusion' | 'context'
    evidence_type: str = ""      # e.g. intent_statement / continued_listing_compliance
    excerpt: str = ""
    exchange: str = ""           # Nasdaq | NYSE | NYSE American | ""
    security_type: str = "unknown"  # common_stock | other | unknown
    weight: int = 0
    notes: list = field(default_factory=list)
    event_date: str = ""         # YYYY-MM-DD when extractable


def detect_exchange(text: str) -> str:
    for pat, name in EXCHANGE_PATTERNS:
        if re.search(pat, text, re.I):
            return name
    return ""


def detect_security_type(text: str, form: str) -> str:
    """What security is being registered / discussed?

    For 8-A filings the registered class is parsed from Item 1
    ("securities to be registered are X" / "registers its X"), ignoring
    references to *underlying* common stock (e.g. "warrants to purchase
    common stock" registers warrants, not common stock).
    """
    blob = text[:20000]
    if form.startswith("8-A"):
        m = re.search(r"item\s+1[.\s].{0,400}?description.{0,200}?", blob, re.I | re.S)
        scope = blob[m.start():m.start() + 3000] if m else blob[:4000]
    else:
        scope = text
    low = scope.lower()

    head = ""
    m = re.search(r"securities\s+to\s+be\s+registered\s+(?:are|is)\s+([^.]{3,120})", low)
    if m:
        head = m.group(1)
    else:
        m = re.search(r"registers?\s+(?:its|the|our)\s+([^.]{3,120})", low)
        if m:
            head = m.group(1)
    if head:
        # strip references to underlying (not registered) common stock
        clean = re.sub(r"to\s+purchase\s+common\s+stock|underlying\s+common\s+stock|"
                       r"exercisable\s+for[^,.]{0,60}|convertible\s+into[^,.]{0,60}", "", head)
        has_c = "common stock" in clean
        has_o = bool(re.search(r"warrant|preferred|note|debenture|debt\s+securit|\bunits?\b|"
                               r"\brights?\b", clean))
        if has_c and has_o:
            return "mixed"
        if has_c:
            return "common_stock"
        if has_o:
            return "other"
        return "unknown"

    has_common = "common stock" in low
    has_other = any(re.search(p, low) for p in OTHER_SECURITY_PATTERNS)
    if has_common and not has_other:
        return "common_stock"
    if has_common and has_other:
        return "mixed"
    if has_other:
        return "other"
    return "unknown"


def _in_risk_factors(text: str, pos: int) -> bool:
    """Heuristic: is position inside a Risk Factors section?"""
    before = text[max(0, pos - 4000):pos]
    heads = [m.start() for m in RISK_FACTOR_HEADS.finditer(before)]
    if not heads:
        return False
    # If a new ITEM heading appears after the last risk-factors head, we're out.
    after_head = before[heads[-1]:]
    return not re.search(r"item\s+\d", after_head, re.I)


def _has(patterns, text) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def classify_hit(window: str, form: str) -> Finding:
    """Classify a single keyword context window."""
    notes = []
    exchange = detect_exchange(window)

    # 1. Hard exclusions first
    if _has(DELISTING_PATTERNS, window):
        return Finding("exclusion", "delisting", window, exchange, notes=["delisting language"])
    if _has(OTC_TIER_PATTERNS, window):
        return Finding("exclusion", "otc_tier_change", window, exchange,
                       notes=["OTC tier change only; no national exchange"])
    if _has(CONTINUED_COMPLIANCE_PATTERNS, window):
        return Finding("exclusion", "continued_listing_compliance", window, exchange,
                       notes=["continued-listing compliance context"])
    if _has(HYPOTHETICAL_PATTERNS, window):
        return Finding("exclusion", "hypothetical", window, exchange,
                       notes=["hypothetical/boilerplate reference"])
    if _has(NEGATION_PATTERNS, window):
        return Finding("context", "negated_or_withdrawn", window, exchange,
                       notes=["negation/withdrawal language near uplist mention"])

    # 2. Positive evidence patterns (ordered by strength)
    for pat, ev_type, weight in rules.POSITIVE_PATTERNS:
        m = re.search(pat, window, re.I)
        if m:
            if ev_type in ("intent_statement",) and _in_risk_factors(window, m.start()):
                notes.append("inside Risk Factors section: treat as boilerplate unless corroborated")
                return Finding("exclusion", "boilerplate", window, exchange,
                               notes=notes)
            # 8-A12B for non-common securities is not uplisting evidence
            if ev_type == "registration_8a":
                sec = detect_security_type(window, form)
                if sec == "other":
                    return Finding("exclusion", "other_security", window, exchange,
                                   notes=["8-A registers non-common security"])
                return Finding("evidence", ev_type, window, exchange, sec, weight, notes)
            sec = detect_security_type(window, form)
            return Finding("evidence", ev_type, window, exchange, sec, weight, notes)

    # 3. Unmatched keyword hit: keep as weak context
    return Finding("context", "keyword_only", window, exchange,
                   notes=["keyword matched but no contextual pattern"])


def extract_event_date(window: str) -> str:
    m = DATE_RE.search(window)
    if not m:
        return ""
    months = {m: i + 1 for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"])}
    try:
        return f"{int(m.group(3)):04d}-{months[m.group(1)]:02d}-{int(m.group(2)):02d}"
    except Exception:
        return ""


def extract_split_ratio(text: str):
    for rx in (SPLIT_RATIO_RE, SPLIT_RATIO_RE2):
        m = rx.search(text)
        if m:
            return f"{m.group(1)}-for-{m.group(2)}"
    return ""


def extract_offering(text: str):
    m = OFFERING_RE.search(text)
    if not m:
        return {}
    out = {"shares": m.group(1).replace(",", "")}
    if m.group(2):
        out["price"] = m.group(2)
    s = max(0, m.start() - 120)
    e = min(len(text), m.end() + 120)
    out["excerpt"] = re.sub(r"\s+", " ", text[s:e]).strip()[:420]
    return out


def analyze_document(text: str, form: str, phrases=None):
    """Full-document analysis -> (findings, doc_meta)."""
    phrases = phrases or rules.SEARCH_PHRASES
    findings = []
    seen_spans = []
    lowered = text.lower()
    for phrase in phrases:
        # word-boundary-ish search, case-insensitive
        pat = re.compile(re.escape(phrase), re.I)
        for m in pat.finditer(text):
            start = max(0, m.start() - WINDOW_CHARS)
            end = min(len(text), m.end() + WINDOW_CHARS)
            # dedupe overlapping windows
            if any(s <= m.start() <= e for s, e in seen_spans):
                continue
            seen_spans.append((start, end))
            window = text[start:end].strip()
            f = classify_hit(window, form)
            f.excerpt = _trim_excerpt(window, phrase)
            f.event_date = extract_event_date(window)
            findings.append(f)
    meta = {
        "split_ratio": extract_split_ratio(text),
        "offering": extract_offering(text),
        "exchange": detect_exchange(text[:50000]),
        "security_type": detect_security_type(text, form),
    }
    return findings, meta


def _trim_excerpt(window: str, phrase: str, max_len=420) -> str:
    """Short exact excerpt centered on the phrase hit."""
    m = re.search(re.escape(phrase), window, re.I)
    if not m:
        return window[:max_len]
    start = max(0, m.start() - 160)
    end = min(len(window), m.end() + 160)
    excerpt = window[start:end].strip()
    if start > 0:
        excerpt = "..." + excerpt
    if end < len(window):
        excerpt = excerpt + "..."
    excerpt = re.sub(r"\s+", " ", excerpt)
    return excerpt[:max_len]


def summarize_findings(findings):
    """Aggregate findings -> counts by type, best evidence, exclusions."""
    evidence = [f for f in findings if f.kind == "evidence"]
    exclusions = [f for f in findings if f.kind == "exclusion"]
    contexts = [f for f in findings if f.kind == "context"]
    by_type = {}
    for f in evidence:
        by_type.setdefault(f.evidence_type, []).append(f)
    return {
        "evidence": evidence,
        "exclusions": exclusions,
        "contexts": contexts,
        "by_type": by_type,
        "n_evidence": len(evidence),
        "n_exclusions": len(exclusions),
    }
