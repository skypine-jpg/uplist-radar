# Verification record — Uplist Radar

All checks below were performed against **live SEC EDGAR data** (no simulated
filings, no mocked outcomes). Unit/edge tests use synthetic fixtures only for
the deterministic analysis layer.

## 1. Live ingestion from real public filings ✅

- EDGAR full-text search (`efts.sec.gov/LATEST/search-index`) queried for 13
  uplisting phrases × 30 forms over 2025-11-04 → 2026-10-30: **2,997 unique
  filings** discovered by accession number.
- Filing indexes, primary documents, and 8-K exhibits (Ex. 99.1) downloaded
  from `www.sec.gov/Archives/edgar/...` with identifying User-Agent, ≤2.5
  req/s throttle, exponential backoff, and on-disk caching.
- Observed and handled: transient SEC timeouts (retried), `exchanges: [null]`
  in submissions JSON (fixed), exhibit-vs-primary document selection (fixed).

## 2. Verified uplisting example (real OTC → Nasdaq) ✅

**Eva Live Inc (GOAI), CIK 0001983736**
- 8-A12B filed 2026-01-27 (accession 0001493152-26-003707): *"This Registration
  Statement on Form 8-A … is being filed … in connection with the **uplisting
  of its common stock**, par value $0.0001 per share …, with The **Nasdaq Stock
  Market** LLC (Nasdaq) under the trading symbol GOAI."*
- Pipeline classification: `registration_8a`, exchange=Nasdaq, common stock —
  correct.
- Prior OTC status: company traded OTC under GOAI before the uplisting (8-A12B
  describes the uplisting *of its common stock* to Nasdaq; SEC submissions now
  lists Nasdaq post-uplisting).
- Stage engine: preparation → (with approval evidence) → approval/trading.

## 3. False-positive rejection ✅

**Odysight.ai Inc (ODYS), CIK 0001577445** — FTS hit for "approved for listing"
(8-K, 2026-03-31). Filing text: *"…approved for listing on the **Tel Aviv
Stock Exchange**… in addition to its existing listing on the **Nasdaq Capital
Market**."* Correctly produced **zero** uplisting evidence (TASE dual listing
is not an OTC→Nasdaq/NYSE uplisting; company already Nasdaq-listed per SEC
submissions). Rejected on two independent grounds.

**Dynamic Aerospace / BrooqLy (BRQL), CIK 0001854526** — 8-K headline
"Uplisting to OTCQB Market" correctly **excluded** as `otc_tier_change`;
separate forward-looking mention of a *potential* uplisting to a national
securities exchange captured as weak `intent_statement` evidence. OTC status
verified via SEC submissions (`exchanges: ["OTC"]`, ticker BRQL).

**NightFood Holdings (NGTF), CIK 0001593001** — genuine candidate: SEC-verified
OTC company whose 8-K discloses *"anticipated uplisting to a U.S. national
securities exchange"* → `intent_statement`.

## 4. Deterministic analysis tests ✅ — `tests/test_analysis.py` (34 checks)

Intent, application filed, conditional approval, trading commencement (incl.
event-date extraction), withdrawal/negation, reverse-split ratios, offering
terms; false-positive rejection for continued-listing compliance, delisting to
OTC, OTC tier changes, boilerplate risk-factor language, hypothetical
statements, and warrant-only 8-A12B (security-type parsing distinguishes
registered vs. underlying common stock). All pass.

## 5. Pipeline edge cases ✅ — `tests/test_pipeline_edge.py` (13 checks)

Duplicate accession dedup, S-1/A → S-1 amendment linking, RW withdrawal →
`delay_withdrawal` evidence + `delayed_withdrawn` stage (chronology: later
withdrawal supersedes earlier intent), stage-history audit rows, failed scans
recorded as failed/partial with `scan_failure` alerts (never "no changes"),
and resumable queue (failed items retried on resume). All pass. Two real bugs
found and fixed by these tests: `stages.STAGE_ORDER` reference and
withdrawal-vs-conflict staging logic.

## 6. Scheduler ✅

- In-process APScheduler: daily 06:00 America/Los_Angeles via IANA timezone
  (DST-correct), editable in Admin UI, reschedules on change.
- Platform cron backstop `uplist-radar-daily-scan`: daily 06:00
  America/Los_Angeles; skips when the app scheduler already completed the day.
- Standalone CLI `scripts/scan_now.py` for cron/systemd; systemd service +
  timer units and Dockerfile provided.

## 7. Traceability ✅

Every company page shows the exact excerpt, filing form/date, accession with
SEC link, evidence-strength rule breakdown, and risk-flag evidence. Stage
changes are audit-logged with actor and reason. Reports link every
company-specific claim to its source filing.

## Pending / not yet activated

- **Licensed market data**: quotes/volume/market-cap are best-effort (Stooq,
  spotty OTC coverage) + SEC XBRL facts. A licensed provider (OTC Markets,
  Polygon, Nasdaq Data Link) must be configured in Admin → Providers for
  production. Missing values display as Unknown — never fabricated.
- **Email delivery**: built, disabled until SMTP is configured.
- **AI enrichment**: deterministic analysis is the default; optional LLM
  summaries require an API key.
- **24/7 operation**: runs while the app process (or platform cron) runs;
  production needs the systemd/Docker deployment.
