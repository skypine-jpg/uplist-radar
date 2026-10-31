"""Versioned rule sets: extraction phrases, scoring, staging.
These are the deterministic rules shown in the UI and recorded on every run.
Bump VERSION_* when changing; db.seed records them in rules_versions.
"""
EXTRACTION_VERSION = "1.0.0"
SCORING_VERSION = "1.0.0"
STAGING_VERSION = "1.0.0"

# Forms monitored (EDGAR form types)
MONITORED_FORMS = [
    "8-K", "8-K/A",
    "S-1", "S-1/A", "S-3", "S-3/A", "F-1", "F-1/A", "F-3", "F-3/A",
    "424B4", "424B5", "424B3",
    "PRE 14A", "DEF 14A", "PRE 14C", "DEF 14C",
    "8-A12B", "8-A12B/A",
    "10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "6-K", "6-K/A",
    "EFFECT", "RW", "RW WD",
]

# Phrases searched in EDGAR full-text search (each queried separately).
SEARCH_PHRASES = [
    "uplisting",
    "up-listing",
    "uplist",
    "Nasdaq Capital Market",
    "NYSE American",
    "national securities exchange",
    "applied to list",
    "listing application",
    "initial listing requirements",
    "conditional approval",
    "approved for listing",
    "reverse stock split",
    "commence trading",
]

# Contextual positive patterns: (regex, evidence_type, weight)
POSITIVE_PATTERNS = [
    (r"uplist(?:ing|ed)?\s+(?:from\s+the\s+OTC\w*\s+)?to\s+(?:the\s+)?(?:Nasdaq|NYSE)", "intent_statement", 3),
    (r"intend(?:s|ed)?\s+to\s+uplist", "intent_statement", 3),
    (r"plan(?:s|ned)?\s+to\s+uplist", "intent_statement", 2),
    (r"applied?\s+to\s+list\s+(?:its\s+)?(?:common\s+stock\s+)?on\s+(?:the\s+)?(?:Nasdaq|NYSE)", "application_filed", 3),
    (r"listing\s+application\s+(?:was\s+)?(?:filed|submitted)", "application_filed", 3),
    (r"conditional(?:ly)?\s+approv\w+\s+(?:for\s+)?listing", "conditional_approval", 4),
    (r"approved\s+for\s+listing\s+on\s+(?:the\s+)?(?:Nasdaq|NYSE)", "approval_notice", 4),
    (r"approved\s+(?:our|the|its)\s+(?:listing\s+)?application", "approval_notice", 3),
    (r"commence\s+trading\s+on\s+(?:the\s+)?(?:Nasdaq|NYSE)", "trading_commencement", 4),
    (r"(?:began|commenced)\s+trading\s+on\s+(?:the\s+)?(?:Nasdaq|NYSE)", "trading_commencement", 4),
    (r"expected\s+to\s+begin\s+trading\s+on\s+(?:the\s+)?(?:Nasdaq|NYSE)", "approval_notice", 3),
    (r"reverse\s+stock\s+split.{0,120}?(?:Nasdaq|NYSE|listing|uplist)", "reverse_split", 2),
    (r"(?:Nasdaq|NYSE)\s+listing\s+requirements", "preparation_activity", 1),
    (r"retain\w*\s+.{0,60}?(?:to\s+assist\s+with|for)\s+.{0,60}?uplist", "preparation_activity", 2),
    (r"8-A12B|Form\s+8-A", "registration_8a", 1),
    (r"national\s+securities\s+exchange", "intent_statement", 1),
]

# Exclusion patterns: (regex, exclusion_reason)
EXCLUSION_PATTERNS = [
    (r"deficiency\s+(?:letter|notice)", "continued_listing_compliance"),
    (r"Nasdaq\s+Listing\s+Qualifications?\s+(?:Department|Panel)", "continued_listing_compliance"),
    (r"minimum\s+bid\s+price\s+(?:requirement|deficiency)", "continued_listing_compliance"),
    (r"regain\s+compliance", "continued_listing_compliance"),
    (r"delist\w*\s+from\s+(?:the\s+)?(?:Nasdaq|NYSE)", "delisting"),
    (r"voluntary\s+delisting", "delisting"),
    (r"transfer\s+(?:of\s+)?(?:its\s+|the\s+)?(?:listing\s+)?(?:from\s+(?:the\s+)?(?:Nasdaq|NYSE)\s+)?to\s+the\s+OTC", "delisting_to_otc"),
    (r"uplist\w*\s+(?:from\s+(?:the\s+)?Pink\s+)?to\s+(?:the\s+)?OTCQB", "otc_tier_change"),
    (r"uplist\w*\s+to\s+(?:the\s+)?OTCQX", "otc_tier_change"),
    (r"approved\s+for\s+quotation\s+on\s+(?:the\s+)?OTC", "otc_tier_change"),
    (r"risk\s+factors?", "boilerplate_context"),  # handled with section logic, not hard exclusion
    (r"there\s+can\s+be\s+no\s+assurance\s+that\s+.*uplist", "hypothetical"),
    (r"may\s+seek\s+to\s+uplist\s+in\s+the\s+future", "hypothetical"),
    (r"no\s+current\s+plans?\s+to\s+uplist", "hypothetical"),
    (r"warrant\s+agreement", "other_security_context"),
]

# Stage definitions with the evidence required to enter each stage.
STAGES = {
    "intent_announced": {
        "label": "Intent announced",
        "description": "Company publicly stated intent/plan to uplist to a national exchange.",
        "requires": ["intent_statement"],
    },
    "preparation": {
        "label": "Preparation underway",
        "description": "Concrete preparatory steps disclosed: reverse split proposal/approval, "
                       "S-1/F-1 filed referencing uplisting, advisors engaged, board/shareholder approvals.",
        "requires": ["preparation_activity", "reverse_split", "registration_8a"],
    },
    "application_submitted": {
        "label": "Application submitted",
        "description": "Company disclosed filing a listing application with Nasdaq/NYSE/NYSE American.",
        "requires": ["application_filed"],
    },
    "conditional_approval": {
        "label": "Conditional approval disclosed",
        "description": "Company disclosed conditional approval of its listing application.",
        "requires": ["conditional_approval"],
    },
    "approval_trading_date": {
        "label": "Approval and trading date announced",
        "description": "Listing approved and expected first-trading date announced.",
        "requires": ["approval_notice"],
    },
    "trading_commenced": {
        "label": "Trading commenced",
        "description": "Common stock confirmed trading on the national exchange.",
        "requires": ["trading_commencement"],
    },
    "delayed_withdrawn": {
        "label": "Delayed, withdrawn, rejected, or abandoned",
        "description": "Uplisting delayed, withdrawn (RW), rejected, or abandoned per filings.",
        "requires": ["delay_withdrawal"],
    },
    "insufficient_evidence": {
        "label": "Insufficient or conflicting evidence",
        "description": "Not enough reliable evidence to assign a substantive stage, or evidence conflicts.",
        "requires": [],
    },
}

STAGE_ORDER = ["intent_announced", "preparation", "application_submitted",
               "conditional_approval", "approval_trading_date", "trading_commenced"]

# Evidence-strength scoring rules: (rule_id, points, description)
SCORING_RULES = [
    ("issuer_intent", 15, "Issuer filing states uplisting intent/plan"),
    ("application_disclosed", 20, "Listing application disclosed as filed"),
    ("exchange_named", 10, "Target exchange named (Nasdaq/NYSE/NYSE American)"),
    ("conditional_approval", 25, "Conditional approval disclosed"),
    ("approval_with_date", 30, "Approval disclosed with expected trading date"),
    ("trading_confirmed", 35, "Trading commencement confirmed"),
    ("exchange_notice", 25, "Independent exchange notice confirms (not just issuer claim)"),
    ("form_8a_common", 10, "8-A12B registers common stock (not warrants/preferred/debt)"),
    ("effect_linked", 10, "EFFECT notice linked to the uplisting registration statement"),
    ("recent_update", 5, "Evidence updated within last 90 days"),
    ("boilerplate_only", -25, "Only boilerplate/hypothetical references found"),
    ("conflicting", -20, "Contradictory statements across filings"),
    ("stale", -10, "No update in over 180 days"),
    ("continued_compliance_ctx", -40, "Context is continued-listing compliance, not uplisting"),
]
SCORE_BANDS = [(75, "Very strong"), (50, "Strong"), (25, "Moderate"), (0, "Weak")]

# Financial/trading risk flags: (flag_id, severity, description, detection)
RISK_FLAG_DEFS = [
    ("dilution_offering", "high", "Registered offering dilutive to existing holders",
     "offering size / shares outstanding computable from filings"),
    ("warrants_attached", "medium", "Warrants attached to offering (overhang)",
     "warrant terms disclosed in S-1/424B prospectus"),
    ("convertible_debt", "medium", "Convertible notes/debt in capital structure",
     "convertible terms disclosed"),
    ("going_concern", "high", "Going-concern qualification in 10-K/10-Q",
     "auditor going-concern paragraph"),
    ("low_cash", "medium", "Cash below $2M or negative working capital",
     "latest balance sheet facts (XBRL or 10-Q/K)"),
    ("negative_equity", "high", "Negative stockholders' equity",
     "latest balance sheet facts"),
    ("reverse_split", "medium", "Reverse split implemented/proposed (often pre-uplisting)",
     "split ratio disclosed"),
    ("resale_registration", "medium", "Selling-stockholder resale registration (S-1 selling holders)",
     "S-1/424B selling stockholder section"),
    ("liquidity_unknown", "low", "No reliable quote/volume data available",
     "market data provider has no coverage"),
    ("stale_evidence", "low", "Uplisting evidence older than 180 days without update",
     "latest evidence date"),
]


def all_rules():
    return [
        ("extraction", EXTRACTION_VERSION, {
            "monitored_forms": MONITORED_FORMS,
            "search_phrases": SEARCH_PHRASES,
            "positive_patterns": POSITIVE_PATTERNS,
            "exclusion_patterns": EXCLUSION_PATTERNS,
        }),
        ("scoring", SCORING_VERSION, {
            "rules": SCORING_RULES,
            "bands": SCORE_BANDS,
            "risk_flags": RISK_FLAG_DEFS,
        }),
        ("staging", STAGING_VERSION, {"stages": STAGES, "order": STAGE_ORDER}),
    ]


def score_band(score: int) -> str:
    for threshold, label in SCORE_BANDS:
        if score >= threshold:
            return label
    return "Weak"
