"""Scan pipeline: Discovery -> Extraction -> Verification -> Change detection.

A scan run:
  1. DISCOVERY: EDGAR full-text search over phrases x forms in the window
     (+ manual watchlist companies' recent filings). Dedupe by accession.
  2. EXTRACTION: download each filing, extract text, run deterministic
     contextual analysis, store filing + evidence + excerpts.
  3. VERIFICATION: resolve company identity (CIK/ticker/name), verify prior
     OTC status via SEC submissions, check security type, flag contradictions.
  4. CHANGE DETECTION: recompute stage/score/flags per company; record
     stage_history and alerts only on real changes.
  5. REPORTING: build the daily report (see reports.py).

Resumable: per-filing work lives in scan_queue; a retry continues pending rows.
A failed run is recorded as failed/partial and never reported as "no changes".
"""
import hashlib
import json
import logging
import re
import traceback
from datetime import date, datetime, timedelta, timezone

from . import config, rules
from .db import utcnow
from . import nlp, stages

log = logging.getLogger("uplist_radar.pipeline")


def norm_cik(cik):
    digits = re.sub(r"\D", "", str(cik or ""))
    return digits.zfill(10) if digits else ""


def _clean_str_list(values):
    """Join helper: SEC sometimes returns [null]; drop non-strings."""
    return ",".join(v for v in (values or []) if isinstance(v, str) and v)


def sic_to_sector(sic):
    """Rough SIC division -> sector label."""
    try:
        code = int(str(sic)[:4])
    except (ValueError, TypeError):
        return "Unknown"
    divisions = [
        (100, 999, "Agriculture/Mining"), (1000, 1499, "Mining"),
        (1500, 1799, "Construction"), (2000, 3999, "Manufacturing"),
        (4000, 4999, "Transportation/Utilities"), (5000, 5199, "Wholesale Trade"),
        (5200, 5999, "Retail Trade"), (6000, 6999, "Finance/Real Estate"),
        (7000, 8999, "Services"), (9100, 9999, "Public Administration"),
    ]
    for lo, hi, label in divisions:
        if lo <= code <= hi:
            return label
    return "Unknown"


def _is_press_exhibit(doc_name):
    return bool(re.match(r"(?i)^ex-?99([.-]1)?\.html?$", doc_name or ""))


def parse_display_name(display):
    """'BROOQLY, Inc.  (BRQL)  (CIK 0001854526)' -> (name, ticker)."""
    name, ticker = display or "", ""
    m = re.search(r"\(([A-Za-z]{1,6})\)\s*\(CIK", name)
    if m:
        ticker = m.group(1).upper()
    name = re.sub(r"\s*\([A-Za-z]{1,6}\)\s*\(CIK.*$", "", name).strip()
    return name, ticker


# ---------------------------------------------------------------- discovery
def discover_accessions(edgar, phrases, forms, start_date, end_date, on_progress=None):
    """FTS discovery. Returns list of dicts (deduped by accession).

    Captures hit_doc from the FTS _id ("adsh:filename") so extraction analyzes
    the exact document the phrase matched (e.g. an 8-K's Ex. 99.1 press release),
    not just the primary filing document.
    """
    seen = {}
    for phrase in phrases:
        count = 0
        try:
            for src, _id in edgar.fts_search(phrase, forms, start_date, end_date):
                adsh = src.get("adsh", "")
                if not adsh:
                    continue
                hit_doc = _id.split(":")[-1] if ":" in str(_id) else ""
                if adsh in seen:
                    # keep the most informative hit doc (prefer press-release exhibits)
                    prev = seen[adsh].get("hit_doc", "")
                    if _is_press_exhibit(hit_doc) and not _is_press_exhibit(prev):
                        seen[adsh]["hit_doc"] = hit_doc
                    continue
                ciks = src.get("ciks") or []
                disp = (src.get("display_names") or [""])[0]
                name, ticker = parse_display_name(disp)
                seen[adsh] = {
                    "accession": adsh,
                    "cik": norm_cik(ciks[0]) if ciks else "",
                    "form": src.get("form") or src.get("file_type") or "",
                    "filing_date": src.get("file_date", ""),
                    "company_name": name,
                    "ticker": ticker,
                    "phrase": phrase,
                    "file_number": (src.get("file_num") or [""])[0],
                    "hit_doc": hit_doc,
                }
                count += 1
        except Exception as e:
            log.warning("FTS query failed for phrase %r: %s", phrase, e)
        if on_progress:
            on_progress(phrase, count, len(seen))
    return list(seen.values())


def queue_watchlist_filings(db, edgar, run_id, window_start, window_end):
    """Queue recent filings for manually added companies (ticker/CIK/name)."""
    queued = 0
    companies = db.query(
        "SELECT * FROM companies WHERE source='manual' AND archived=0")
    forms = [f for f in rules.MONITORED_FORMS if f not in ("EFFECT",)]
    for c in companies:
        try:
            subs = edgar.submissions(c["cik"])
        except Exception as e:
            log.warning("submissions failed for %s: %s", c["cik"], e)
            continue
        recent = subs.get("filings", {}).get("recent", {})
        n = len(recent.get("accessionNumber", []))
        for i in range(n):
            form = recent.get("form", [""])[i]
            if form not in rules.MONITORED_FORMS:
                continue
            fdate = recent.get("filingDate", [""])[i]
            if not (window_start <= fdate <= window_end):
                continue
            acc = recent.get("accessionNumber", [""])[i].replace("-", "")
            acc = f"{acc[0:10]}-{acc[10:12]}-{acc[12:]}"
            exists = db.query_one("SELECT id FROM filings WHERE accession=?", (acc,))
            if exists:
                continue
            db.execute(
                "INSERT OR IGNORE INTO scan_queue(run_id,accession,cik,form,filing_date) "
                "VALUES(?,?,?,?,?)",
                (run_id, acc, c["cik"], form, fdate))
            queued += 1
    return queued


# ---------------------------------------------------------------- extraction
def _amendment_link(db, cik, form, filing_date, file_number):
    """Link an /A filing to its original via (cik, base form, file_number)."""
    base = form.replace("/A", "")
    row = db.query_one(
        """SELECT accession FROM filings WHERE cik=? AND replace(form,'/A','')=?
           AND filing_date<=? AND (?='' OR file_number=? OR file_number='')
           ORDER BY filing_date DESC LIMIT 1""",
        (cik, base, filing_date, file_number or "", file_number or ""))
    return row["accession"] if row else None


def process_filing(db, edgar, run_id, item):
    """Fetch, extract, classify one filing. Returns dict summary."""
    accession = item["accession"]
    cik = norm_cik(item.get("cik") or "")
    form = item.get("form") or ""
    filing_date = item.get("filing_date") or ""

    if db.query_one("SELECT id FROM filings WHERE accession=?", (accession,)):
        db.execute("UPDATE scan_queue SET status='done' WHERE run_id=? AND accession=?",
                   (run_id, accession))
        return {"status": "skipped", "reason": "already ingested"}

    # Identify company (CIK required; resolve via submissions when missing)
    company_name = item.get("company_name") or ""
    ticker = (item.get("ticker") or "").upper()
    if not cik:
        raise ValueError("no CIK on FTS hit")
    try:
        subs = edgar.submissions(cik)
    except Exception as e:
        raise RuntimeError(f"submissions fetch failed: {e}")
    if not company_name:
        company_name = subs.get("name", f"CIK {cik}")
    tickers = subs.get("tickers") or []
    if not ticker and tickers:
        ticker = (tickers[0] or "").upper()

    # Filing index -> documents to analyze: the FTS hit doc first (often an
    # exhibit like Ex. 99.1), plus the primary doc and 8-K press exhibits.
    try:
        index_names = [i["name"] for i in
                       edgar.filing_index(cik, accession)["directory"]["item"]]
    except Exception as e:
        raise RuntimeError(f"filing index failed: {e}")
    docs = []
    hit_doc = item.get("hit_doc") or ""
    if hit_doc and hit_doc in index_names:
        docs.append(hit_doc)
    primary = edgar.primary_doc_name(cik, accession, form_hint=form)
    if primary and primary not in docs:
        docs.append(primary)
    if form in ("8-K", "8-K/A"):
        for n in index_names:
            if _is_press_exhibit(n) and n not in docs:
                docs.append(n)
                break
    if not docs:
        raise RuntimeError("no analyzable documents in filing index")

    findings, metas, texts = [], [], []
    for doc_name in docs:
        path, content = edgar.fetch_document(cik, accession, doc_name)
        text = edgar.html_to_text(content)
        if len(text) < 200:
            continue
        f, m = nlp.analyze_document(text, form)
        findings.extend(f)
        metas.append(m)
        texts.append(text)
    if not texts:
        raise RuntimeError("document text too short/empty")
    # merge meta: first non-empty wins per key
    meta = {}
    for m in metas:
        for k, v in m.items():
            if v and not meta.get(k):
                meta[k] = v
    # dedupe identical excerpts across docs
    seen_ex = set()
    findings = [f for f in findings
                if not (f.excerpt in seen_ex or seen_ex.add(f.excerpt))]
    summary = nlp.summarize_findings(findings)
    content_hash = hashlib.sha256("\n".join(texts).encode("utf-8", "ignore")).hexdigest()
    is_amend = 1 if form.endswith("/A") else 0
    file_number = item.get("file_number") or ""
    amends = _amendment_link(db, cik, form, filing_date, file_number) if is_amend else None

    doc_name = docs[0]
    path = edgar.cache_path(cik, accession, doc_name)
    try:
        filing_id = db.execute(
            """INSERT INTO filings(accession,cik,form,filing_date,primary_doc,title,url,
               file_number,is_amendment,amends_accession,content_hash,cache_path,fetched_at,
               extra_docs)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (accession, cik, form, filing_date, doc_name,
             f"{company_name} {form} {filing_date}",
             edgar.document_url(cik, accession, doc_name),
             file_number, is_amend, amends, content_hash, str(path), utcnow(),
             json.dumps(docs[1:]) if len(docs) > 1 else None))
    except Exception as e:
        if "UNIQUE constraint failed: filings.accession" in str(e):
            # Already ingested (e.g. reprocessed after a restart); skip gracefully.
            db.execute("UPDATE scan_queue SET status='done' WHERE run_id=? AND accession=?",
                       (run_id, accession))
            return {"status": "skipped", "reason": "already ingested"}
        raise

    # Upsert company
    sic = str(subs.get("sic") or "")
    sic_desc = subs.get("sicDescription") or ""
    sector = sic_to_sector(sic)
    company = db.query_one("SELECT * FROM companies WHERE cik=?", (cik,))
    if company is None:
        company_id = db.execute(
            """INSERT INTO companies(cik,ticker,name,source,current_exchange,last_checked_at,
               sic,sic_description,sector,state_of_incorp,website)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (cik, ticker, company_name, "discovery",
             _clean_str_list(subs.get("exchanges")), utcnow(), sic, sic_desc, sector,
             subs.get("stateOfIncorporation"), subs.get("website")))
    else:
        company_id = company["id"]
        db.execute("UPDATE companies SET ticker=COALESCE(NULLIF(ticker,''),?), "
                   "name=?, current_exchange=?, last_checked_at=?, updated_at=?, "
                   "sic=?, sic_description=?, sector=? WHERE id=?",
                   (ticker, company_name, _clean_str_list(subs.get("exchanges")),
                    utcnow(), utcnow(), sic, sic_desc, sector, company_id))

    # Store evidence (RW/EFFECT get special handling below)
    n_evidence = 0
    for f in findings:
        if f.kind == "context" and f.evidence_type == "keyword_only":
            continue  # weak hits are not stored as evidence
        ev_type = f.evidence_type
        is_excluded = 1 if f.kind == "exclusion" else 0
        db.execute(
            """INSERT INTO evidence(company_id,filing_id,evidence_type,excerpt,context,
               exchange_mentioned,security_type,event_date,is_excluded,exclusion_reason,
               extractor_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (company_id, filing_id, ev_type, f.excerpt,
             "; ".join(f.notes), f.exchange, f.security_type, f.event_date,
             is_excluded, ev_type if is_excluded else "",
             rules.EXTRACTION_VERSION))
        n_evidence += 1

    # Financing terms evidence (offering size/price from prospectus text)
    if meta.get("offering"):
        off = meta["offering"]
        detail = f"Offering of {off['shares']} shares" + \
                 (f" at ${off['price']}" if off.get("price") else "")
        db.execute(
            """INSERT INTO evidence(company_id,filing_id,evidence_type,excerpt,context,
               exchange_mentioned,security_type,event_date,is_excluded,exclusion_reason,
               extractor_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (company_id, filing_id, "financing_terms", off.get("excerpt", ""),
             detail, "", "unknown", "", 0, "", rules.EXTRACTION_VERSION))

    # Special forms
    if form in ("RW", "RW WD"):
        _link_withdrawal(db, company_id, filing_id, cik, file_number, "\n".join(texts))
    elif form == "EFFECT":
        _link_effect(db, company_id, filing_id, cik, file_number)

    db.execute("UPDATE scan_queue SET status='done' WHERE run_id=? AND accession=?",
               (run_id, accession))
    return {"status": "ok", "company_id": company_id, "filing_id": filing_id,
            "n_evidence": n_evidence,
            "n_positive": summary["n_evidence"], "n_excluded": summary["n_exclusions"]}


def _link_withdrawal(db, company_id, filing_id, cik, file_number, text):
    """RW: link to the withdrawn registration by file number; record delay/withdrawal evidence."""
    target = None
    if file_number:
        row = db.query_one(
            "SELECT id FROM filings WHERE cik=? AND file_number=? AND form LIKE 'S-1%' "
            "ORDER BY filing_date DESC LIMIT 1", (cik, file_number))
        if row:
            target = row["id"]
    # Check whether the withdrawn registration was uplisting-related
    prior = db.query(
        """SELECT DISTINCT evidence_type FROM evidence e JOIN filings f ON f.id=e.filing_id
           WHERE e.company_id=? AND e.is_excluded=0
           AND f.form IN ('S-1','S-1/A','F-1','F-1/A')""", (company_id,))
    related = {r["evidence_type"] for r in prior} - {"keyword_only"}
    excerpt = nlp._trim_excerpt(text[:2000] + text[-2000:], "withdraw", 420)
    db.execute(
        """INSERT INTO evidence(company_id,filing_id,evidence_type,excerpt,context,
           exchange_mentioned,security_type,event_date,is_excluded,exclusion_reason,
           extractor_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (company_id, filing_id, "delay_withdrawal", excerpt,
         "RW withdrawal request" + (" linked to uplisting registration" if related else
                                    " (registration not linked to uplisting evidence)"),
         "", "unknown", "", 0, "", rules.EXTRACTION_VERSION))


def _link_effect(db, company_id, filing_id, cik, file_number):
    """EFFECT: link to the effective registration by file number."""
    if not file_number:
        return
    row = db.query_one(
        "SELECT id FROM filings WHERE cik=? AND file_number=? AND id!=? "
        "ORDER BY filing_date DESC LIMIT 1", (cik, file_number, filing_id))
    if not row:
        return
    prior = db.query_one(
        """SELECT COUNT(*) c FROM evidence WHERE filing_id=? AND is_excluded=0
           AND evidence_type IN ('intent_statement','application_filed','preparation_activity',
           'reverse_split','registration_8a','conditional_approval','approval_notice')""",
        (row["id"],))
    if prior and prior["c"] > 0:
        db.execute(
            """INSERT INTO evidence(company_id,filing_id,evidence_type,excerpt,context,
               exchange_mentioned,security_type,event_date,is_excluded,exclusion_reason,
               extractor_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (company_id, filing_id, "effect_linked",
             f"EFFECT notice for registration file no. {file_number}",
             "registration statement declared effective; linked to uplisting-related filing",
             "", "unknown", "", 0, "", rules.EXTRACTION_VERSION))


# ---------------------------------------------------------------- verification
def verify_company_otc(db, edgar, company):
    """Verify prior OTC status from SEC submissions (authoritative for exchange)."""
    cik = company["cik"]
    try:
        subs = edgar.submissions(cik)
    except Exception as e:
        return {"otc_status": "unknown", "note": f"submissions fetch failed: {e}"}
    exchanges = subs.get("exchanges") or []
    tickers = [t for t in (subs.get("tickers") or []) if isinstance(t, str) and t]
    if "OTC" in exchanges:
        note = f"SEC submissions lists exchange=OTC, ticker(s)={','.join(tickers)}"
        db.execute("UPDATE companies SET otc_status='verified_otc', otc_evidence=?, "
                   "ticker=COALESCE(NULLIF(ticker,''),?), current_exchange=? WHERE id=?",
                   (note, tickers[0] if tickers else company["ticker"],
                    _clean_str_list(exchanges), company["id"]))
        return {"otc_status": "verified_otc", "note": note}
    if any(x in ("Nasdaq", "NYSE", "NYSE American") for x in exchanges):
        note = f"SEC submissions lists exchange={_clean_str_list(exchanges)} — already listed"
        db.execute("UPDATE companies SET otc_status='not_otc', otc_evidence=? WHERE id=?",
                   (note, company["id"]))
        return {"otc_status": "not_otc", "note": note}
    # No exchange listed: infer from filing text evidence
    ev = db.query_one(
        """SELECT excerpt FROM evidence WHERE company_id=? AND is_excluded=0
           AND lower(excerpt) LIKE '%otc%' LIMIT 1""", (company["id"],))
    if ev:
        note = "OTC quotation referenced in filed documents"
        db.execute("UPDATE companies SET otc_status='likely_otc', otc_evidence=? WHERE id=?",
                   (note, company["id"]))
        return {"otc_status": "likely_otc", "note": note}
    db.execute("UPDATE companies SET otc_status='unknown', otc_evidence=? WHERE id=?",
               ("no exchange data from SEC submissions or filings", company["id"]))
    return {"otc_status": "unknown", "note": "no exchange data"}


# ---------------------------------------------------------------- change detection
def recompute_company(db, edgar, company_id, actor="system"):
    """Recompute stage/score/flags; record history + alerts on change."""
    c = db.query_one("SELECT * FROM companies WHERE id=?", (company_id,))
    ev_rows = db.query(
        """SELECT e.*, f.accession AS filing_accession, f.filing_date, f.form
           FROM evidence e JOIN filings f ON f.id=e.filing_id
           WHERE e.company_id=? ORDER BY f.filing_date, f.id""", (company_id,))
    evidence = [dict(r) for r in ev_rows]
    positive = [e for e in evidence if not e["is_excluded"]]
    types = [e["evidence_type"] for e in positive]

    # Chronology decides: a later withdrawal supersedes earlier intent (it is
    # progress, not a contradiction). Genuine conflict = contradictory claims
    # within the SAME filing with no clear recency.
    new_stage, reason = "insufficient_evidence", "no substantive uplisting evidence"
    if positive:
        ordered = sorted(positive, key=lambda e: (e.get("filing_date") or "", e["id"]))
        neg_types = {"negated_or_withdrawn", "delay_withdrawal"}
        latest = ordered[-1]
        if latest["evidence_type"] in neg_types:
            new_stage = "delayed_withdrawn"
            reason = (f"latest evidence ({latest['filing_accession']}, "
                      f"{latest['filing_date']}) discloses withdrawal/delay")
        else:
            fwd_types = [t for t in types
                         if stages.EVIDENCE_TO_STAGE.get(t) not in (None, "delayed_withdrawn")]
            acc_by_type = {}
            for e in ordered:
                acc_by_type.setdefault(e["evidence_type"], set()).add(e["filing_accession"])
            same_filing = any(acc_by_type.get(nt, set()) & acc_by_type.get(ft, set())
                              for nt in neg_types for ft in fwd_types)
            if same_filing:
                new_stage = "insufficient_evidence"
                reason = "contradictory statements within the same filing"
            else:
                new_stage, reason = stages.assign_stage(fwd_types)
                if any(t in neg_types for t in types) and new_stage in rules.STAGE_ORDER:
                    reason += ("; note: an earlier withdrawal/delay was disclosed but "
                               "superseded by later evidence")

    score, band, breakdown = stages.score_evidence(positive)
    filings_meta = []
    for r in db.query("SELECT DISTINCT f.id FROM filings f JOIN evidence e ON e.filing_id=f.id "
                      "WHERE e.company_id=?", (company_id,)):
        # meta already extracted at ingest; re-derive cheaply from excerpts is skip;
        # risk flags use stored meta via filings text scan below
        pass
    # gather split/offering meta from filing cache (cheap: stored at ingest time would be
    # better; re-scan cached text)
    metas = []
    for frow in db.query("SELECT cache_path, form FROM filings WHERE cik=?", (c["cik"],)):
        try:
            if frow["cache_path"]:
                import pathlib
                p = pathlib.Path(frow["cache_path"])
                if p.exists():
                    t = edgar.html_to_text(p.read_bytes())
                    metas.append({"split_ratio": nlp.extract_split_ratio(t),
                                  "offering": nlp.extract_offering(t)})
        except Exception:
            pass
    market = db.query_one(
        "SELECT * FROM market_snapshots WHERE company_id=? ORDER BY as_of DESC LIMIT 1",
        (company_id,))
    flags = stages.detect_risk_flags(dict(c), positive, metas,
                                     dict(market) if market else {})

    # target exchange: most-mentioned
    exch = ""
    for e in positive:
        if e.get("exchange_mentioned"):
            exch = e["exchange_mentioned"]
            break
    # proposed ticker: look for "trade under the symbol" in excerpts
    proposed = c["proposed_ticker"] or ""
    if not proposed:
        for e in positive:
            m = re.search(r"under\s+the\s+symbol\s+[\"']?([A-Z]{1,6})[\"']?", e.get("excerpt") or "",
                          re.I)
            if m:
                proposed = m.group(1).upper()
                break

    old_stage = c["stage"]
    db.execute(
        """UPDATE companies SET stage=?, stage_updated_at=?, evidence_strength=?,
           evidence_breakdown=?, risk_flags=?, target_exchange=?, proposed_ticker=?,
           last_checked_at=?, updated_at=? WHERE id=?""",
        (new_stage, utcnow(), score, json.dumps(breakdown), json.dumps(flags),
         exch or "Unknown", proposed, utcnow(), utcnow(), company_id))
    changed = (old_stage != new_stage)
    if changed:
        ev_id = positive[-1]["id"] if positive else None
        db.execute(
            "INSERT INTO stage_history(company_id,from_stage,to_stage,reason,evidence_id,actor)"
            " VALUES(?,?,?,?,?,?)",
            (company_id, old_stage, new_stage, reason, ev_id, actor))
        db.execute(
            """INSERT INTO alerts(company_id,alert_type,title,body,filing_id)
               VALUES(?, 'stage_change', ?, ?, ?)""",
            (company_id,
             f"{c['ticker'] or c['name']}: {rules.STAGES[old_stage]['label']} → "
             f"{rules.STAGES[new_stage]['label']}",
             reason, ev_id and db.query_one(
                 "SELECT filing_id FROM evidence WHERE id=?", (ev_id,))["filing_id"]))
    db.audit(actor, "company_recomputed", "company", str(company_id),
             f"stage {old_stage}->{new_stage} score={score}")
    return {"stage": new_stage, "changed": changed, "score": score, "band": band,
            "flags": flags, "reason": reason}


# ---------------------------------------------------------------- run
class ScanRun:
    def __init__(self, db, edgar, mode="manual", window_start=None, window_end=None,
                 resume_run_id=None, logger=None):
        self.db = db
        self.edgar = edgar
        self.mode = mode
        self.log = logger or log
        self.window_start, self.window_end = self._window(window_start, window_end, mode)
        self.resume_run_id = resume_run_id

    def _window(self, ws, we, mode):
        today = date.today()
        if ws and we:
            return ws, we
        if mode == "initial":
            months = int(self.db.get_setting("initial_window_months", "12"))
            ws = (today - timedelta(days=30 * months)).isoformat()
        else:
            # incremental with overlap
            last = self.db.query_one(
                "SELECT window_end FROM scan_runs WHERE status IN ('success','partial') "
                "ORDER BY id DESC LIMIT 1")
            overlap = int(self.db.get_setting("scan_overlap_days",
                                              str(config.SCAN_OVERLAP_DAYS)))
            if last and last["window_end"]:
                ws = (datetime.fromisoformat(last["window_end"]).date()
                      - timedelta(days=overlap)).isoformat()
            else:
                ws = (today - timedelta(days=30)).isoformat()
        return ws, today.isoformat()

    def run(self):
        db = self.db
        if self.resume_run_id:
            run_id = self.resume_run_id
            db.execute("UPDATE scan_runs SET status='running', error=NULL WHERE id=?",
                       (run_id,))
        else:
            run_id = db.execute(
                """INSERT INTO scan_runs(mode,window_start,window_end,forms,queries,status)
                   VALUES(?,?,?,?,?,'running')""",
                (self.mode, self.window_start, self.window_end,
                 json.dumps(rules.MONITORED_FORMS), json.dumps(rules.SEARCH_PHRASES)))
        log_path = str(config.LOGS_DIR / f"scan_{run_id}.log")
        db.execute("UPDATE scan_runs SET log_path=? WHERE id=?", (log_path, run_id))
        fh = logging.FileHandler(log_path)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger("uplist_radar").addHandler(fh)

        stats = {"found": 0, "fetched": 0, "new_companies": 0, "stage_changes": 0,
                 "errors": []}
        touched_companies = set()
        try:
            # --- discovery
            self.log.info("Discovery: %s..%s phrases=%d", self.window_start,
                          self.window_end, len(rules.SEARCH_PHRASES))
            if not self.resume_run_id:
                hits = discover_accessions(
                    self.edgar, rules.SEARCH_PHRASES, rules.MONITORED_FORMS,
                    self.window_start, self.window_end,
                    on_progress=lambda ph, c, t: self.log.info(
                        "FTS phrase %r: %d new hits (%d total)", ph, c, t))
                stats["found"] = len(hits)
                for h in hits:
                    db.execute(
                        "INSERT OR IGNORE INTO scan_queue(run_id,accession,cik,form,filing_date,hit_doc)"
                        " VALUES(?,?,?,?,?,?)",
                        (run_id, h["accession"], h["cik"], h["form"], h["filing_date"],
                         h.get("hit_doc") or ""))
                stats["watchlist_queued"] = queue_watchlist_filings(
                    db, self.edgar, run_id, self.window_start, self.window_end)
            db.execute("UPDATE scan_runs SET filings_found=? WHERE id=?",
                       (db.query_one("SELECT COUNT(*) c FROM scan_queue WHERE run_id=?",
                                     (run_id,))["c"], run_id))

            # --- verification + change detection per touched company.
            # Batched every 25 filings so staging/progress survive a crash or
            # restart; a final pass covers any remainder.
            def _verify_batch(cids):
                for cid in cids:
                    c = db.query_one("SELECT * FROM companies WHERE id=?", (cid,))
                    if not c:
                        continue
                    verify_company_otc(db, self.edgar, dict(c))
                    # Exclude already-listed companies whose only evidence is
                    # compliance context
                    c2 = db.query_one("SELECT * FROM companies WHERE id=?", (cid,))
                    if c2["otc_status"] == "not_otc":
                        excl = db.query_one(
                            "SELECT COUNT(*) c FROM evidence WHERE company_id=? "
                            "AND is_excluded=0", (cid,))["c"]
                        if excl == 0:
                            db.execute("UPDATE companies SET archived=1 WHERE id=?",
                                       (cid,))
                            db.audit("system", "company_archived", "company",
                                     str(cid),
                                     "already exchange-listed; no uplisting evidence")
                            continue
                    res = recompute_company(db, self.edgar, cid)
                    if res["changed"]:
                        stats["stage_changes"] += 1
                    # new company alert
                    n_ev = db.query_one(
                        "SELECT COUNT(*) c FROM evidence WHERE company_id=? "
                        "AND is_excluded=0", (cid,))["c"]
                    if n_ev > 0:
                        already = db.query_one(
                            "SELECT id FROM alerts WHERE company_id=? "
                            "AND alert_type='new_candidate'", (cid,))
                        if not already:
                            db.execute(
                                "INSERT INTO alerts(company_id,alert_type,title,body) "
                                "VALUES(?, 'new_candidate', ?, ?)",
                                (cid, f"New candidate: {c2['ticker'] or c2['name']} "
                                       f"({c2['cik']})",
                                 f"Stage: {rules.STAGES[res['stage']]['label']}; "
                                 f"evidence strength {res['score']}/100 "
                                 f"({res['band']})."))

            # --- extraction + verification per filing (resumable)
            pending = db.query(
                "SELECT * FROM scan_queue WHERE run_id=? AND status IN ('pending','failed') "
                "ORDER BY filing_date DESC", (run_id,))
            for n, item in enumerate(pending):
                item = dict(item)
                try:
                    if item["attempts"] >= 3:
                        db.execute("UPDATE scan_queue SET status='skipped' WHERE id=?",
                                   (item["id"],))
                        continue
                    db.execute("UPDATE scan_queue SET attempts=attempts+1 WHERE id=?",
                               (item["id"],))
                    res = process_filing(db, self.edgar, run_id, item)
                    if res["status"] == "ok":
                        stats["fetched"] += 1
                        touched_companies.add(res["company_id"])
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    self.log.warning("Filing %s failed: %s", item["accession"], err)
                    db.execute("UPDATE scan_queue SET status='failed', error=? WHERE id=?",
                               (err[:500], item["id"]))
                    stats["errors"].append({"accession": item["accession"], "error": err})
                # Per-batch verification disabled for speed; the final pass
                # below stages all touched companies. Evidence is safe in DB.
                pass

            # Final pass for any companies touched since the last batch.
            _verify_batch(touched_companies)
            touched_companies.clear()

            new_companies = db.query_one(
                "SELECT COUNT(*) c FROM companies WHERE date(created_at) >= date(?)",
                (self.window_start,))["c"]
            stats["new_companies"] = new_companies

            # --- finalize
            failed = db.query_one(
                "SELECT COUNT(*) c FROM scan_queue WHERE run_id=? AND status='failed'",
                (run_id,))["c"]
            status = "success" if not stats["errors"] and failed == 0 else \
                ("partial" if stats["fetched"] > 0 else "failed")
            n_excluded = db.query_one(
                """SELECT COUNT(*) c FROM evidence e JOIN filings f ON f.id=e.filing_id
                   JOIN scan_queue q ON q.accession=f.accession
                   WHERE q.run_id=? AND e.is_excluded=1""", (run_id,))["c"]
            db.execute(
                """UPDATE scan_runs SET finished_at=?, status=?, filings_fetched=?,
                   new_companies=?, stage_changes=?, error=?,
                   filings_failed=?, filings_excluded=? WHERE id=?""",
                (utcnow(), status, stats["fetched"], stats["new_companies"],
                 stats["stage_changes"],
                 json.dumps(stats["errors"][:20]) if stats["errors"] else None,
                 failed, n_excluded, run_id))
            db.audit("system", "scan_finished", "scan_run", str(run_id),
                     f"status={status} fetched={stats['fetched']} "
                     f"changes={stats['stage_changes']}")
            if status == "failed":
                db.execute(
                    "INSERT INTO alerts(alert_type,title,body) VALUES("
                    "'scan_failure','Scan run failed','Run #%s failed with %d errors. "
                    "This is NOT reported as \"no changes\". See Admin > Scheduler.')",
                    (run_id, len(stats["errors"])))
        except Exception as e:
            tb = traceback.format_exc()
            self.log.error("Scan run %s crashed: %s\n%s", run_id, e, tb)
            db.execute("UPDATE scan_runs SET finished_at=?, status='failed', error=? "
                       "WHERE id=?", (utcnow(), f"{type(e).__name__}: {e}"[:1000], run_id))
            db.execute(
                "INSERT INTO alerts(alert_type,title,body) VALUES("
                "'scan_failure','Scan run crashed',?)",
                (f"Run #{run_id}: {e}. This is NOT reported as 'no changes'.",))
            stats["errors"].append({"fatal": str(e)})
        finally:
            logging.getLogger("uplist_radar").removeHandler(fh)
        return run_id, stats
