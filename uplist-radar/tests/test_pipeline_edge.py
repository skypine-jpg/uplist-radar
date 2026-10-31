"""Edge-case integration tests with a stubbed EDGAR client (no network).

Covers: duplicate accession dedup, amendment linking, RW withdrawal linkage,
failed-scan handling (never reported as "no changes"), and queue resume.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from uplist_radar.db import DB
from uplist_radar import pipeline
from uplist_radar.pipeline import ScanRun

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))


class FakeEdgar:
    """Canned EDGAR responses; records calls."""

    DOCS = {
        ("0001111111", "0000000000-26-000001", "form8-k.htm"):
            b"<html><body><p>Item 8.01 Other Events. On May 1, 2026, the board of directors "
            b"of Fake OTC Corp approved a plan to uplist our common stock from the OTCQB "
            b"venture market to the Nasdaq Capital Market. We intend to file a listing "
            b"application with Nasdaq during the third quarter, subject to meeting the "
            b"initial listing requirements, including a potential reverse stock split.</p></body></html>",
        ("0001111111", "0000000000-26-000002", "forms-1.htm"):
            b"<html><body><p>Registration statement on Form S-1 filed in connection with "
            b"the planned uplisting of our common stock to the Nasdaq Capital Market. "
            b"The offering described herein is being conducted to satisfy Nasdaq initial "
            b"listing requirements and for general corporate purposes.</p></body></html>",
        ("0001111111", "0000000000-26-000003", "forms-1a.htm"):
            b"<html><body><p>Amendment No. 1 to the Form S-1 registration statement "
            b"regarding the planned uplisting of our common stock to the Nasdaq Capital "
            b"Market. This amendment updates the prospectus summary and risk factors.</p></body></html>",
        ("0001111111", "0000000000-26-000004", "rw.htm"):
            b"<html><body><p>Pursuant to Rule 477, the Company respectfully requests "
            b"withdrawal of the registration statement on Form S-1 (File No. 333-123456). "
            b"No securities were sold. The Company is withdrawing its listing application "
            b"with Nasdaq and does not intend to proceed with the uplisting at this time.</p></body></html>",
    }

    def filing_index(self, cik, accession):
        docs = [d for (c, a, d) in self.DOCS if c == cik and a == accession]
        return {"directory": {"item": [{"name": d} for d in docs]}}

    def primary_doc_name(self, cik, accession, form_hint=""):
        from uplist_radar.edgar import EdgarClient
        # reuse real heuristic against the fake index
        real = EdgarClient.__new__(EdgarClient)
        real.filing_index = self.filing_index
        return EdgarClient.primary_doc_name(real, cik, accession, form_hint)

    def fetch_document(self, cik, accession, doc_name):
        content = self.DOCS[(cik, accession, doc_name)]
        p = Path(tempfile.gettempdir()) / "fake_edgar" / doc_name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(content)
        return p, content

    def cache_path(self, cik, accession, doc_name):
        return Path(tempfile.gettempdir()) / "fake_edgar" / doc_name

    def document_url(self, cik, accession, doc_name):
        return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc_name}"

    def html_to_text(self, content):
        from uplist_radar.edgar import EdgarClient
        return EdgarClient.html_to_text(content)

    def submissions(self, cik):
        return {"name": "Fake OTC Corp", "tickers": ["FAKE"], "exchanges": ["OTC"],
                "sic": "7374", "sicDescription": "Services-Computer Processing",
                "stateOfIncorporation": "NV", "website": ""}


def fresh_db():
    tmp = tempfile.mktemp(suffix=".db")
    db = DB(tmp)
    db.init()
    return db


def queue_item(run_id, accession, form, filing_date="2026-05-01"):
    return {"id": 1, "run_id": run_id, "accession": accession, "cik": "0001111111",
            "form": form, "filing_date": filing_date, "hit_doc": "",
            "status": "pending", "attempts": 0}


# --- 1. ingest one filing, then re-ingest (duplicate) -> skipped
db = fresh_db()
edgar = FakeEdgar()
run_id = db.execute("INSERT INTO scan_runs(mode,status) VALUES('manual','running')")
item = queue_item(run_id, "0000000000-26-000001", "8-K")
res1 = pipeline.process_filing(db, edgar, run_id, item)
check("ingest ok", res1["status"] == "ok", str(res1))
res2 = pipeline.process_filing(db, edgar, run_id, item)
check("duplicate skipped", res2["status"] == "skipped", str(res2))
check("one filing row", db.query_one("SELECT COUNT(*) c FROM filings")["c"] == 1)
check("evidence stored", db.query_one("SELECT COUNT(*) c FROM evidence WHERE is_excluded=0")["c"] >= 1)

# --- 2. amendment links to original
item2 = queue_item(run_id, "0000000000-26-000002", "S-1")
pipeline.process_filing(db, edgar, run_id, item2)
item3 = queue_item(run_id, "0000000000-26-000003", "S-1/A")
pipeline.process_filing(db, edgar, run_id, item3)
amend = db.query_one("SELECT amends_accession, is_amendment FROM filings WHERE accession=?",
                     ("0000000000-26-000003",))
check("amendment flagged", amend["is_amendment"] == 1)
check("amendment linked", amend["amends_accession"] == "0000000000-26-000002",
      str(dict(amend)))

# --- 3. RW withdrawal creates delay/withdrawal evidence
item4 = queue_item(run_id, "0000000000-26-000004", "RW")
pipeline.process_filing(db, edgar, run_id, item4)
wd = db.query_one("SELECT COUNT(*) c FROM evidence WHERE evidence_type='delay_withdrawal'")
check("RW -> delay_withdrawal evidence", wd["c"] >= 1, str(wd["c"]))
cid = db.query_one("SELECT id FROM companies WHERE cik='0001111111'")["id"]
out = pipeline.recompute_company(db, edgar, cid)
check("withdrawal latest -> delayed_withdrawn", out["stage"] == "delayed_withdrawn",
      f"stage={out['stage']}")
check("stage history recorded",
      db.query_one("SELECT COUNT(*) c FROM stage_history WHERE company_id=?", (cid,))["c"] >= 1)

# --- 4. failed scan is recorded as failed + alert (never "no changes")
db2 = fresh_db()


class BoomEdgar(FakeEdgar):
    def filing_index(self, *a, **k):
        raise RuntimeError("simulated SEC outage")


run2 = ScanRun(db2, BoomEdgar(), mode="manual", window_start="2026-10-01",
               window_end="2026-10-30")
# seed one queue item directly to force per-filing failure path
rid = db2.execute("INSERT INTO scan_runs(mode,status,window_start,window_end) VALUES('manual','running','2026-10-01','2026-10-30')")
db2.execute("INSERT INTO scan_queue(run_id,accession,cik,form,filing_date) VALUES(?,?,?,?,?)",
            (rid, "0000000000-26-000009", "0001111111", "8-K", "2026-10-05"))
run2.resume_run_id = rid
run_id_done, stats = run2.run()
st = db2.query_one("SELECT status FROM scan_runs WHERE id=?", (rid,))["status"]
check("failed scan recorded", st in ("failed", "partial"), f"status={st}")
check("failure alert raised",
      db2.query_one("SELECT COUNT(*) c FROM alerts WHERE alert_type='scan_failure'")["c"] >= 1)
check("no fake success", st != "success" or stats["fetched"] > 0, f"status={st}")

# --- 5. resume continues pending/failed items (resumable jobs)
db3 = fresh_db()
rid3 = db3.execute("INSERT INTO scan_runs(mode,status) VALUES('manual','running')")
db3.execute("INSERT INTO scan_queue(run_id,accession,cik,form,filing_date,status,attempts) VALUES(?,?,?,?,?,'failed',1)",
            (rid3, "0000000000-26-000001", "0001111111", "8-K", "2026-05-01"))
run3 = ScanRun(db3, FakeEdgar(), mode="retry", resume_run_id=rid3,
               window_start="2026-10-01", window_end="2026-10-30")
run3.run()
q = db3.query_one("SELECT status FROM scan_queue WHERE run_id=?", (rid3,))
check("resume processes failed item", q["status"] == "done", f"status={q['status']}")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
