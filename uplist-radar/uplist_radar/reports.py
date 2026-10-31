"""Daily report generation: Markdown -> HTML/PDF/CSV.

Every company-specific claim links to its supporting SEC filing. If the scan
failed or was incomplete, the report says so explicitly — it never renders
"no changes" for a failed scan.
"""
import csv
import io
import json
from datetime import date
from pathlib import Path

from . import config, rules
from .db import utcnow


def _sec_link(accession, cik):
    adsh = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh}/"


def build_daily_report(db, run_id):
    """Build and persist the daily report for a scan run. Returns report id."""
    run = db.query_one("SELECT * FROM scan_runs WHERE id=?", (run_id,))
    report_date = (run["window_end"] or date.today().isoformat())
    status = run["status"]

    lines = []
    lines.append(f"# Uplist Radar — Daily Report ({report_date})")
    lines.append("")
    lines.append(f"_Run #{run_id} · mode={run['mode']} · status={status}_")
    lines.append("")
    lines.append("## Scan coverage")
    lines.append(f"- Window: {run['window_start']} → {run['window_end']}")
    forms = json.loads(run["forms"] or "[]")
    lines.append(f"- Forms monitored: {len(forms)} ({', '.join(forms[:12])}…)")
    lines.append(f"- Filings discovered: {run['filings_found']}; fetched & analyzed: {run['filings_fetched']}")
    if run["started_at"]:
        lines.append(f"- Completed: {run['finished_at'] or 'incomplete'}")
    lines.append("")

    if status == "failed":
        lines.append("## ⚠️ Scan failed")
        lines.append("This scan did not complete successfully. **Results below are not "
                     "reported as 'no changes' — the scan must be retried.**")
        if run["error"]:
            lines.append(f"- Error: `{run['error'][:500]}`")
        lines.append("")
    elif status == "partial":
        lines.append("## ⚠️ Scan partially completed")
        lines.append("Some filings failed to process (see Admin > Scheduler for the error log). "
                     "Findings below cover only successfully processed filings.")
        lines.append("")

    # New candidates
    new_c = db.query(
        """SELECT c.* FROM companies c WHERE c.archived=0 AND c.stage != 'insufficient_evidence'
           AND date(c.created_at) >= date(?) ORDER BY c.evidence_strength DESC""",
        (run["window_start"],))
    lines.append("## Newly discovered candidates")
    if new_c:
        for c in new_c:
            ev = db.query_one(
                """SELECT e.excerpt, f.accession, f.form, f.filing_date
                   FROM evidence e JOIN filings f ON f.id=e.filing_id
                   WHERE e.company_id=? AND e.is_excluded=0
                   ORDER BY f.filing_date DESC LIMIT 1""", (c["id"],))
            link = _sec_link(ev["accession"], c["cik"]) if ev else ""
            lines.append(f"- **{c['ticker'] or '?'}** — {c['name']} "
                         f"(`{c['cik']}`) → {c['target_exchange']} · "
                         f"stage: **{rules.STAGES[c['stage']]['label']}** · "
                         f"evidence {c['evidence_strength']}/100")
            if ev:
                lines.append(f"  - Evidence ({ev['form']}, filed {ev['filing_date']}): "
                             f"[{ev['accession']}]({link}) — “{ev['excerpt'][:220]}”")
    else:
        lines.append("_None in this window._")
    lines.append("")

    # Stage changes
    changes = db.query(
        """SELECT sh.*, c.ticker, c.name, c.cik FROM stage_history sh
           JOIN companies c ON c.id=sh.company_id
           WHERE date(sh.changed_at) >= date(?) ORDER BY sh.changed_at DESC""",
        (run["window_start"],))
    lines.append("## Material stage changes")
    if changes:
        for ch in changes:
            lines.append(f"- **{ch['ticker'] or ch['name']}**: "
                         f"{rules.STAGES[ch['from_stage']]['label'] if ch['from_stage'] else '—'} → "
                         f"**{rules.STAGES[ch['to_stage']]['label']}** — {ch['reason']}")
    else:
        lines.append("_None._" if status == "success" else
                     "_Not assessed — scan incomplete._")
    lines.append("")

    # Financing / reverse splits / dilution
    lines.append("## New financing, reverse-split & dilution disclosures")
    fin = db.query(
        """SELECT c.ticker, c.name, c.cik, e.excerpt, f.form, f.filing_date, f.accession
           FROM evidence e JOIN companies c ON c.id=e.company_id
           JOIN filings f ON f.id=e.filing_id
           WHERE e.is_excluded=0 AND e.evidence_type IN ('reverse_split')
           AND date(f.filing_date) >= date(?) AND c.archived=0
           ORDER BY f.filing_date DESC LIMIT 25""", (run["window_start"],))
    if fin:
        for r in fin:
            lines.append(f"- **{r['ticker'] or r['name']}** ({r['form']} {r['filing_date']}): "
                         f"[{r['accession']}]({_sec_link(r['accession'], r['cik'])}) — "
                         f"“{r['excerpt'][:200]}”")
    else:
        lines.append("_None in this window._")
    lines.append("")

    # Announced trading dates
    lines.append("## Announced trading dates")
    td = db.query(
        """SELECT c.ticker, c.name, e.event_date, f.filing_date, f.accession, c.cik
           FROM evidence e JOIN companies c ON c.id=e.company_id
           JOIN filings f ON f.id=e.filing_id
           WHERE e.is_excluded=0 AND e.event_date != '' AND e.event_date >= date('now')
           AND c.archived=0 ORDER BY e.event_date LIMIT 25""")
    if td:
        for r in td:
            lines.append(f"- **{r['ticker'] or r['name']}**: expected {r['event_date']} "
                         f"(disclosed {r['filing_date']}, [{r['accession']}]"
                         f"({_sec_link(r['accession'], r['cik'])}))")
    else:
        lines.append("_None upcoming._")
    lines.append("")

    # Delays / withdrawals / conflicts
    lines.append("## Delays, withdrawals & conflicting evidence")
    dw = db.query(
        """SELECT c.ticker, c.name, c.cik, e.excerpt, f.form, f.filing_date, f.accession,
                  e.evidence_type
           FROM evidence e JOIN companies c ON c.id=e.company_id
           JOIN filings f ON f.id=e.filing_id
           WHERE e.evidence_type IN ('delay_withdrawal','negated_or_withdrawn')
           AND date(f.filing_date) >= date(?) AND c.archived=0
           ORDER BY f.filing_date DESC LIMIT 25""", (run["window_start"],))
    if dw:
        for r in dw:
            lines.append(f"- **{r['ticker'] or r['name']}** ({r['form']} {r['filing_date']}): "
                         f"[{r['accession']}]({_sec_link(r['accession'], r['cik'])}) — "
                         f"“{r['excerpt'][:200]}”")
    else:
        lines.append("_None in this window._")
    lines.append("")

    # Manual review
    lines.append("## Items requiring manual review")
    rev = db.query(
        """SELECT c.ticker, c.name, c.cik, c.stage, c.otc_status FROM companies c
           WHERE c.archived=0 AND (c.stage='insufficient_evidence' OR c.otc_status='unknown')
           ORDER BY c.updated_at DESC LIMIT 25""")
    if rev:
        for r in rev:
            lines.append(f"- **{r['ticker'] or r['name']}** (`{r['cik']}`): stage="
                         f"{rules.STAGES[r['stage']]['label']}, OTC status={r['otc_status']}")
    else:
        lines.append("_None._")
    lines.append("")

    if status == "success" and not new_c and not changes and not fin and not dw:
        lines.append("> No material changes detected in this scan window.")
        lines.append("")

    lines.append("---")
    lines.append("_Uplist Radar · sources: SEC EDGAR (primary), exchange notices, company IR. "
                 "Market data best-effort unless a licensed provider is configured. "
                 "Not investment advice._")

    markdown = "\n".join(lines)
    title = f"Daily uplisting report — {report_date}"

    html_path = str(config.REPORTS_DIR / f"report_{run_id}.html")
    pdf_path = str(config.REPORTS_DIR / f"report_{run_id}.pdf")
    csv_path = str(config.REPORTS_DIR / f"report_{run_id}_candidates.csv")
    Path(html_path).write_text(_markdown_to_html(markdown, title))
    _write_pdf(markdown, title, pdf_path, status)
    _write_candidates_csv(db, csv_path, run["window_start"])

    report_id = db.execute(
        """INSERT INTO reports(scan_run_id,report_date,title,markdown,html_path,pdf_path,csv_path)
           VALUES(?,?,?,?,?,?,?)""",
        (run_id, report_date, title, markdown, html_path, pdf_path, csv_path))
    return report_id


def _markdown_to_html(markdown_text, title):
    import markdown as md_lib
    body = md_lib.markdown(markdown_text)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;
line-height:1.55;color:#1a1a1a}}a{{color:#0b5fff}}h1,h2{{border-bottom:1px solid #ddd;
padding-bottom:.3rem}}blockquote{{border-left:3px solid #0b5fff;padding-left:1rem;color:#333}}
code{{background:#f4f4f4;padding:0 .3rem;border-radius:3px}}</style></head>
<body>{body}</body></html>"""


def _write_pdf(markdown_text, title, pdf_path, status):
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.units import inch
    import re as _re
    styles = getSampleStyleSheet()
    story = [Paragraph(title, styles["Title"]), Spacer(1, 0.2 * inch)]
    # crude markdown -> paragraphs
    for line in markdown_text.split("\n"):
        line = line.strip()
        if not line:
            story.append(Spacer(1, 0.08 * inch))
            continue
        if line.startswith("# "):
            story.append(Paragraph(_re.sub(r"^#+\s*", "", line), styles["Heading1"]))
        elif line.startswith("## "):
            story.append(Paragraph(line[3:], styles["Heading2"]))
        elif line.startswith("- ") or line.startswith("> "):
            txt = _re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line[2:])
            txt = _re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", txt)
            txt = _re.sub(r"“|”", '"', txt)
            story.append(Paragraph("• " + txt, styles["BodyText"]))
        elif line == "---":
            story.append(Spacer(1, 0.15 * inch))
        else:
            txt = _re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", line)
            story.append(Paragraph(txt, styles["BodyText"]))
    SimpleDocTemplate(pdf_path, pagesize=LETTER,
                      leftMargin=0.8 * inch, rightMargin=0.8 * inch).build(story)


def _write_candidates_csv(db, csv_path, since):
    rows = db.query(
        """SELECT c.ticker, c.cik, c.name, c.target_exchange, c.stage, c.evidence_strength,
                  c.otc_status, c.proposed_ticker, c.last_checked_at
           FROM companies c WHERE c.archived=0 ORDER BY c.evidence_strength DESC""")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "cik", "name", "target_exchange", "stage",
                    "evidence_strength", "otc_status", "proposed_ticker", "last_checked"])
        for r in rows:
            w.writerow([r["ticker"], r["cik"], r["name"], r["target_exchange"],
                        rules.STAGES[r["stage"]]["label"], r["evidence_strength"],
                        r["otc_status"], r["proposed_ticker"], r["last_checked_at"]])
