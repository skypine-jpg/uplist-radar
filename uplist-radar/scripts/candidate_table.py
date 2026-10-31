#!/usr/bin/env python3
"""Verified candidate table for the initial scan.

One record per company with filings as chronological evidence. Ranks verified
OTC common-stock candidates by documented stage, then evidence strength and
unresolved conditions. Pending cases kept separate from historical completed
uplistings. Writes scripts/candidate_table.md.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from uplist_radar.db import DB
from uplist_radar import stages

db = DB()
out = []

def w(s=""):
    out.append(s)

run = db.query_one("SELECT * FROM scan_runs WHERE id=2")
q = db.query_one(
    "SELECT COUNT(*) c, COALESCE(SUM(status='done'),0) d, "
    "COALESCE(SUM(status='failed'),0) f FROM scan_queue WHERE run_id=2")

w("# Uplist Radar — verified candidate table")
w(f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
  f"scan run #2 status: {run['status']}_")
w("")
w("## Scan coverage")
w(f"- Window: {run['window_start']} → {run['window_end']}")
w(f"- Filings discovered: {run['filings_found']} (queue: {q['c']})")
w(f"- Successfully analyzed: {q['d']}")
w(f"- Failed: {run['filings_failed']} (queue failed: {q['f']})")
w(f"- Excluded (false-positive forms): {run['filings_excluded']}")
w(f"- Unique companies in DB (not archived): "
  f"{db.query_one('SELECT COUNT(*) c FROM companies WHERE archived=0')['c']}")
w(f"- Unique companies archived (already listed / excluded): "
  f"{db.query_one('SELECT COUNT(*) c FROM companies WHERE archived=1')['c']}")
w("")

# per-stage counts
w("## Companies per stage (active, not archived)")
import uplist_radar.rules as rules
for st_key in rules.STAGE_ORDER:
    n = db.query_one(
        "SELECT COUNT(*) c FROM companies WHERE archived=0 AND stage=?", (st_key,))["c"]
    otc = db.query_one(
        "SELECT COUNT(*) c FROM companies WHERE archived=0 AND stage=? "
        "AND otc_status='verified_otc'", (st_key,))["c"]
    if n:
        w(f"- {rules.STAGES[st_key]['label']}: {n} (verified OTC: {otc})")
w("")

# candidate records, ranked: group rank, then strength, then fewer outstanding
cands = db.query(
    """SELECT * FROM companies WHERE archived=0 AND stage NOT IN
       ('insufficient_evidence','trading_commenced','delayed_withdrawn')
       ORDER BY evidence_strength DESC""")
records = []
for c in cands:
    c = dict(c)
    ev = [dict(r) for r in db.query(
        """SELECT e.*, f.form, f.filing_date, f.accession AS filing_accession, f.url
           FROM evidence e JOIN filings f ON f.id=e.filing_id
           WHERE e.company_id=? ORDER BY f.filing_date DESC""", (c["id"],))]
    ev_fwd = [e for e in ev if not e["is_excluded"]]
    if not ev_fwd:
        continue
    items, freshness = stages.build_checklist(ev)
    outstanding = [i for i in items if i["status"] == "outstanding"]
    grp = stages.group_for_stage(c["stage"])
    # strongest supporting evidence for the assigned stage
    stage_types = {t for t, s in stages.EVIDENCE_TO_STAGE.items() if s == c["stage"]}
    key_ev = next((e for e in ev_fwd if e["evidence_type"] in stage_types), ev_fwd[0])
    url = (key_ev.get("url") or "").replace(
        ".htm", "-index.htm") if key_ev.get("url") else ""
    records.append({
        "c": c, "grp": grp, "outstanding": outstanding, "freshness": freshness,
        "key_ev": key_ev, "flags": json.loads(c["risk_flags"] or "[]"),
        "n_filings": db.query_one(
            "SELECT COUNT(*) c FROM filings WHERE cik=?", (c["cik"],))["c"],
    })

records.sort(key=lambda r: (r["grp"]["rank"], -r["c"]["evidence_strength"],
                             len(r["outstanding"])))

w(f"## Ranked candidates ({len(records)} with qualifying evidence)")
w("")
top = records[:10]
for i, r in enumerate(top, 1):
    c, grp = r["c"], r["grp"]
    ke = r["key_ev"]
    w(f"### {i}. {c['name']} ({c['ticker'] or 'ticker unknown'}, CIK {c['cik']})")
    w(f"- **Group:** {grp['label']} · **Stage strength:** {c['evidence_strength']}/100")
    w(f"- **Target exchange:** {c['target_exchange'] or 'Unknown'} · "
      f"**OTC status:** {c['otc_status']}")
    w(f"- **Latest supported stage:** {rules.STAGES[c['stage']]['label']}")
    w(f"- **Key evidence** ({ke['form']}, filed {ke['filing_date']}): "
      f"“{ke['excerpt'][:400]}”")
    if ke.get("filing_accession"):
        w(f"  Source: https://www.sec.gov/Archives/edgar/data/{int(c['cik'])}/"
          f"{ke['filing_accession'].replace('-', '')}/ "
          f"(accession {ke['filing_accession']})")
    w(f"- **Event date:** {ke.get('event_date') or 'Unknown'} · "
      f"**Last verified:** {(c['last_checked_at'] or 'Unknown')[:16]} · "
      f"**Evidence freshness:** "
      f"{r['freshness']} days since latest evidence"
      f"{' — STALE' if r['freshness'] and r['freshness'] > 180 else ''}")
    if r["outstanding"]:
        w(f"- **Outstanding ({len(r['outstanding'])}):** " +
          "; ".join(f"{o['item']} — {o['detail'][:140]}" for o in r["outstanding"]))
    else:
        w("- **Outstanding:** none identified in reviewed filings")
    risks = [f"{f['flag']} ({f['severity']}): {f['detail'][:120]}"
             for f in r["flags"][:5]]
    w(f"- **Principal financial risks:** " + ("; ".join(risks) if risks
        else "no risk flags extracted — data gap, not a clean bill of health"))
    w(f"- **Next verifiable milestone:** {stages.NEXT_MILESTONE.get(c['stage'], '')}")
    hr = db.query_one(
        "SELECT reason FROM stage_history WHERE company_id=? ORDER BY changed_at DESC LIMIT 1",
        (c["id"],))
    w(f"- **Why this stage:** {(hr['reason'] if hr else None) or 'n/a'}")
    w("")

# completed + withdrawn, kept separate
for label, st_key in [("Completed — trading commenced (historical archive)",
                       stages.COMPLETED_STAGE),
                      ("Withdrawn / rejected (separate outcome)",
                       stages.WITHDRAWN_STAGE)]:
    rows = db.query(
        "SELECT ticker, name, cik, evidence_strength, stage_updated_at FROM companies "
        "WHERE archived=0 AND stage=? ORDER BY stage_updated_at DESC", (st_key,))
    w(f"## {label}: {len(rows)}")
    for r_ in rows:
        w(f"- {r_['ticker'] or '?'} — {r_['name'][:60]} (strength {r_['evidence_strength']})")
    w("")

Path(__file__).parent.joinpath("candidate_table.md").write_text("\n".join(out))
print("\n".join(out[:40]))
print(f"\n... full table written to scripts/candidate_table.md "
      f"({len(records)} candidates, top {len(top)} shown)")
