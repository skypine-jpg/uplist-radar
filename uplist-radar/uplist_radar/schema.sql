-- Uplist Radar database schema (SQLite)
-- Versioned; see rules_versions for scoring/extraction rule versions.

PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cik TEXT NOT NULL UNIQUE,              -- 10-digit zero-padded
    ticker TEXT,                            -- OTC ticker, e.g. BRQL
    name TEXT NOT NULL,
    sector TEXT,                            -- derived from SIC division
    sic TEXT,
    sic_description TEXT,
    state_of_incorp TEXT,
    website TEXT,
    source TEXT NOT NULL DEFAULT 'discovery',  -- discovery | manual
    otc_status TEXT,                        -- verified_otc | likely_otc | not_otc | unknown
    otc_evidence TEXT,                      -- short note on how OTC status was verified
    current_exchange TEXT,                  -- from SEC submissions (e.g. OTC, Nasdaq)
    target_exchange TEXT,                   -- Nasdaq | NYSE | NYSE American | Unknown
    proposed_ticker TEXT,
    stage TEXT NOT NULL DEFAULT 'insufficient_evidence',
    stage_updated_at TEXT,
    stage_evidence_id INTEGER,
    evidence_strength INTEGER NOT NULL DEFAULT 0,   -- 0..100
    evidence_breakdown TEXT,                -- JSON list of {rule, points, note}
    risk_flags TEXT,                        -- JSON list of {flag, severity, evidence}
    last_checked_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    archived INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS filings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    accession TEXT NOT NULL UNIQUE,         -- 0001234567-25-000001
    cik TEXT NOT NULL,
    form TEXT NOT NULL,
    filing_date TEXT,                       -- YYYY-MM-DD
    report_date TEXT,
    primary_doc TEXT,
    title TEXT,
    url TEXT,
    file_number TEXT,
    is_amendment INTEGER NOT NULL DEFAULT 0,
    amends_accession TEXT,                  -- linked original filing, when known
    content_hash TEXT,                      -- sha256 of extracted text
    cache_path TEXT,                        -- local cached document path
    fetched_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_filings_cik ON filings(cik);
CREATE INDEX IF NOT EXISTS idx_filings_form ON filings(form);
CREATE INDEX IF NOT EXISTS idx_filings_date ON filings(filing_date);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    filing_id INTEGER NOT NULL REFERENCES filings(id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL,            -- intent_statement | application_filed | ...
    excerpt TEXT NOT NULL,                  -- short exact quote
    context TEXT,                           -- surrounding analysis note
    exchange_mentioned TEXT,
    security_type TEXT,                     -- common_stock | other | unknown
    event_date TEXT,
    is_excluded INTEGER NOT NULL DEFAULT 0,
    exclusion_reason TEXT,
    extractor_version TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_evidence_company ON evidence(company_id);

CREATE TABLE IF NOT EXISTS stage_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    from_stage TEXT,
    to_stage TEXT NOT NULL,
    changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    reason TEXT,
    evidence_id INTEGER REFERENCES evidence(id),
    actor TEXT NOT NULL DEFAULT 'system'    -- system | username
);
CREATE INDEX IF NOT EXISTS idx_stage_history_company ON stage_history(company_id);

CREATE TABLE IF NOT EXISTS watchlist (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    notify INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, company_id)
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running', -- running | success | partial | failed
    mode TEXT NOT NULL DEFAULT 'manual',    -- initial | scheduled | manual | retry
    window_start TEXT,
    window_end TEXT,
    forms TEXT,                             -- JSON list
    queries TEXT,                           -- JSON list
    filings_found INTEGER NOT NULL DEFAULT 0,
    filings_fetched INTEGER NOT NULL DEFAULT 0,
    new_companies INTEGER NOT NULL DEFAULT 0,
    stage_changes INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    log_path TEXT
);

-- Resumable per-filing work queue for a scan run.
CREATE TABLE IF NOT EXISTS scan_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
    accession TEXT NOT NULL,
    cik TEXT,
    form TEXT,
    filing_date TEXT,
    status TEXT NOT NULL DEFAULT 'pending', -- pending | done | failed | skipped
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    UNIQUE(run_id, accession)
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_run_id INTEGER REFERENCES scan_runs(id),
    report_date TEXT NOT NULL,              -- YYYY-MM-DD
    title TEXT NOT NULL,
    markdown TEXT NOT NULL,
    html_path TEXT,
    pdf_path TEXT,
    csv_path TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_reports_date ON reports(report_date);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    report_id INTEGER REFERENCES reports(id) ON DELETE SET NULL,
    alert_type TEXT NOT NULL,               -- new_candidate | stage_change | financing | trading_date | delay_withdrawal | conflict | review_needed | scan_failure
    title TEXT NOT NULL,
    body TEXT,
    filing_id INTEGER REFERENCES filings(id) ON DELETE SET NULL,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_alerts_read ON alerts(is_read);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    as_of TEXT NOT NULL,
    provider TEXT NOT NULL,
    price REAL,
    currency TEXT,
    volume INTEGER,
    market_cap REAL,
    shares_outstanding REAL,
    quote_time TEXT,
    is_delayed INTEGER,
    raw_json TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,               -- edgar | stooq | otc_markets | polygon | ...
    name TEXT NOT NULL,
    kind TEXT NOT NULL,                     -- filings | market_data | ai
    status TEXT NOT NULL DEFAULT 'unknown', -- ok | degraded | not_configured | error
    config_json TEXT,
    last_used_at TEXT,
    usage_json TEXT
);

CREATE TABLE IF NOT EXISTS secrets (
    key TEXT PRIMARY KEY,
    ciphertext TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS rules_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,                     -- extraction | scoring | staging
    version TEXT NOT NULL,
    content_json TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(name, version)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity TEXT,
    entity_id TEXT,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
