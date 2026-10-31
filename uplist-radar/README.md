# Uplist Radar

Agentic research portal that discovers and monitors publicly traded OTC companies
preparing to list on **Nasdaq, NYSE, or NYSE American** — from public SEC filings,
with evidence-linked stages, daily reports, and alerts.

> Not investment advice. All findings link to their source filings. Figures that
> cannot be verified are shown as **Unknown**, never fabricated.

## What it does

- **Daily discovery scan** (default 06:00 America/Los_Angeles, DST-correct) over
  SEC EDGAR full-text search across 13 uplisting phrases × 30 filing forms
  (8-K, S-1/S-1/A, F-1, 424B4/B5, PRE/DEF 14A/14C, 8-A12B, 10-K/Q, 20-F, 6-K,
  EFFECT, RW…), plus monitoring of manually added companies (ticker/name/CIK).
- **Agent workflow**: Discovery → Extraction (deterministic contextual NLP) →
  Verification (OTC status via SEC submissions, security-type checks) →
  Change detection (stage history, dedup by accession) → Reporting.
- **Evidence-based stages**: intent announced → preparation → application
  submitted → conditional approval → approval & trading date → trading commenced
  → delayed/withdrawn → insufficient evidence. Every stage links to the exact
  filing excerpt.
- **Two separate assessments**: evidence strength (0–100, rule breakdown shown)
  and financial/trading risk flags. No invented approval probabilities.
- **Daily report** (HTML/PDF/CSV) with scan coverage, new candidates, stage
  changes, financing/dilution disclosures, trading dates, delays, and items
  needing manual review. Searchable archive. In-app alerts; optional email.
- **Admin**: scheduler status/next run/failure history, retry of failed runs
  (resumable per-filing queue), provider configuration, encrypted secrets,
  versioned rules, audit trail.

## Quick start (local)

```bash
cd uplist-radar
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python - <<'EOF'
from uplist_radar.db import DB
DB().init()
EOF
.venv/bin/python scripts/create_admin.py <username> <password>
# REQUIRED by SEC policy: set a real contact email (Admin > SEC access, or env)
export UPLIST_SEC_CONTACT="you@example.com"
.venv/bin/python -m uplist_radar.app   # http://127.0.0.1:8765  (scheduler starts too)
```

Then in **Admin**: press **Initial 12-month scan** (or `Scan now` for incremental).
The scan runs in the background; progress is on the Admin page.

Standalone scan (cron/systemd, no web server):

```bash
.venv/bin/python scripts/scan_now.py --mode scheduled   # or --mode initial
```

## Production deployment

1. **Server**: use `Dockerfile` (`docker build/run`, set `UPLIST_SEC_CONTACT`,
   `UPLIST_MASTER_KEY`, `UPLIST_FLASK_SECRET`) or `systemd/uplist-radar.service`
   (runs the web app; the in-process APScheduler fires the daily 6:00 AM
   America/Los_Angeles scan — DST handled via IANA timezone).
2. **Alternative**: disable the in-app scheduler and use
   `systemd/uplist-radar-scan.timer` (daily 06:00) with `scripts/scan_now.py`.
   Use one or the other, not both.
3. **Reverse proxy + TLS**: put nginx/Caddy in front; the app binds 127.0.0.1:8765.
4. **First run**: create admin, set SEC contact email, run the initial 12-month scan.

## What is operational vs. what needs configuration

| Feature | Status |
|---|---|
| SEC EDGAR ingestion (FTS, submissions, documents) | ✅ Operational — free, no key; **requires contact email in User-Agent** (SEC policy) |
| Discovery / extraction / staging / change detection | ✅ Operational, deterministic rules v1.0.0 |
| Daily 6:00 AM PT scheduler (DST-correct) | ✅ Operational in-app; needs the app process running (systemd/Docker for 24/7) |
| Scan-now, editable schedule, retry/resume | ✅ Operational |
| Reports (HTML/PDF/CSV), archive, in-app alerts | ✅ Operational |
| Auth, private watchlists/notes, audit trail, encrypted secrets | ✅ Operational |
| **Market data (quotes, volume, market cap)** | ⚠️ **Best-effort only** — Stooq (free, spotty OTC coverage) + SEC XBRL facts. **A licensed provider (OTC Markets, Polygon, Nasdaq Data Link) is required for production**; configure in Admin → Providers. Missing values show as Unknown. |
| **Email delivery** | ⚠️ Built, **disabled until configured** (Admin → Email; SMTP password stored encrypted) |
| **AI enrichment (LLM summaries)** | ⚠️ Interface stubbed; deterministic analysis is the default. Requires an API key in Providers → openai |

## Data coverage & gaps (honest)

- **Universe**: all SEC EDGAR filers. Discovery searches filing *text* for
  uplisting phrases — companies that never mention uplisting in a filing are
  not discovered. Non-SEC filers (no 10-K/8-K obligation) have thinner coverage.
- **Not claimed**: we do not scan "every OTC company"; we scan EDGAR filings
  for uplisting evidence, then verify OTC status per candidate.
- **Amendments** are preserved as separate linked filings (S-1/A → S-1).
- **Dedup** by SEC accession number; incremental scans overlap 7 days.
- **Rate limiting**: ≤ ~2.5 req/s to SEC with exponential backoff; documents
  cached under `data/filings/`.

## Repository layout

```
uplist_radar/        # package
  app.py             # Flask web app + routes
  pipeline.py        # scan orchestration (discovery→…→change detection)
  edgar.py           # SEC client (polite, cached, retrying)
  nlp.py             # deterministic contextual analysis
  stages.py          # stage assignment, evidence scoring, risk flags
  rules.py           # versioned rule sets (extraction/scoring/staging)
  marketdata.py      # quote + XBRL adapters, provider interface
  reports.py         # daily report → HTML/PDF/CSV
  scheduler.py       # APScheduler daily job + optional email
  auth.py / secrets.py / db.py / config.py
  templates/ static/ # Jinja UI
scripts/             # scan_now.py, create_admin.py, run.sh
systemd/             # service + timer units
tests/               # deterministic analysis tests
```

## Verification performed

- `tests/test_analysis.py`: 29 checks covering uplisting intent, application,
  conditional approval, trading commencement, withdrawal, reverse splits,
  offerings — and false-positive rejection (continued-listing compliance,
  delisting-to-OTC, OTC tier changes, boilerplate, hypothetical, warrant-only
  8-A). All pass.
- Live EDGAR ingestion verified (see `docs/VERIFICATION.md` after the initial scan).

## Security notes

- Passwords: pbkdf2_hmac (200k iterations). Secrets (SMTP/API keys): Fernet,
  key from `UPLIST_MASTER_KEY` env or generated `data/.master_key` (0600).
- Filing content is treated as untrusted input; no instructions in documents
  are followed.
- Change `admin` credentials and set `UPLIST_FLASK_SECRET` before exposing
  the app beyond localhost.
