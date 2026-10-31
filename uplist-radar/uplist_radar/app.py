"""Uplist Radar web app: dashboard, candidates, company pages, reports, admin."""
import csv
import functools
import io
import json
import threading
from datetime import date, datetime, timezone

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, Response, session, url_for)

from . import config, rules
from .auth import authenticate, create_user
from .db import DB, utcnow
from .edgar import EdgarClient, EdgarError
from .pipeline import ScanRun, norm_cik, verify_company_otc, recompute_company
from .reports import build_daily_report
from .scheduler import get_schedule, reschedule, run_scheduled_scan, start as start_scheduler
from .secrets import delete_secret, get_secret, list_secret_keys, set_secret
from . import marketdata

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY or "dev-only-change-me"
_db = None


def db():
    global _db
    if _db is None:
        _db = DB()
        _db.init()
    return _db


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


def current_user():
    if "user_id" not in session:
        return None
    return db().query_one("SELECT * FROM users WHERE id=?", (session["user_id"],))


@app.before_request
def _guard():
    if request.path.startswith("/static/") or request.path in ("/login", "/healthz"):
        return
    if "user_id" not in session:
        return redirect(url_for("login", next=request.path))


@app.template_filter("fromjson")
def _fromjson(v):
    try:
        return json.loads(v or "{}")
    except Exception:
        return {}


@app.context_processor
def _inject():
    d = db()
    unread = d.query_one("SELECT COUNT(*) c FROM alerts WHERE is_read=0")["c"]
    return {"unread_alerts": unread, "STAGES": rules.STAGES,
            "now": datetime.now(timezone.utc)}


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "version": config.APP_VERSION})


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = authenticate(db(), request.form["username"], request.form["password"])
        if user:
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            db().audit(user["username"], "login", "user", str(user["id"]))
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- dashboard
@app.route("/")
@login_required
def dashboard():
    d = db()
    from . import stages as _st
    # Five priority groups + completed archive + withdrawn outcome.
    # Stage advances only on new qualifying evidence, never on filing volume.
    groups = []
    for g in _st.STAGE_GROUPS:
        rows = d.query(
            f"""SELECT * FROM companies WHERE archived=0 AND stage IN
                ({','.join('?' * len(g['stages']))})
                ORDER BY evidence_strength DESC""", g["stages"])
        groups.append({"meta": g, "companies": [dict(r) for r in rows]})
    completed = d.query(
        "SELECT * FROM companies WHERE archived=0 AND stage=? ORDER BY stage_updated_at DESC",
        (_st.COMPLETED_STAGE,))
    withdrawn = d.query(
        "SELECT * FROM companies WHERE archived=0 AND stage=? ORDER BY stage_updated_at DESC",
        (_st.WITHDRAWN_STAGE,))
    new_disc = d.query(
        """SELECT * FROM companies WHERE archived=0 AND stage != 'insufficient_evidence'
           AND date(created_at) >= date('now','-7 days') ORDER BY evidence_strength DESC LIMIT 10""")
    stage_changes = d.query(
        """SELECT sh.*, c.ticker, c.name, c.id AS company_id FROM stage_history sh
           JOIN companies c ON c.id=sh.company_id
           ORDER BY sh.changed_at DESC LIMIT 10""")
    upcoming = d.query(
        """SELECT c.id, c.ticker, c.name, e.event_date, f.filing_date, f.accession, c.cik
           FROM evidence e JOIN companies c ON c.id=e.company_id
           JOIN filings f ON f.id=e.filing_id
           WHERE e.is_excluded=0 AND e.event_date != '' AND e.event_date >= date('now')
           AND c.archived=0 ORDER BY e.event_date LIMIT 10""")
    watch = d.query(
        """SELECT c.* FROM watchlist w JOIN companies c ON c.id=w.company_id
           WHERE w.user_id=? AND c.archived=0 ORDER BY c.evidence_strength DESC""",
        (session["user_id"],))
    delayed = d.query(
        """SELECT * FROM companies WHERE archived=0 AND stage='delayed_withdrawn'
           ORDER BY stage_updated_at DESC LIMIT 10""")
    last_run = d.query_one("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1")
    failures = d.query(
        "SELECT * FROM scan_runs WHERE status IN ('failed','partial') "
        "ORDER BY id DESC LIMIT 5")
    counts = {
        "candidates": d.query_one("SELECT COUNT(*) c FROM companies WHERE archived=0 "
                                  "AND stage != 'insufficient_evidence'")["c"],
        "watchlist": len(watch),
        "unread": d.query_one("SELECT COUNT(*) c FROM alerts WHERE is_read=0")["c"],
    }
    scan_time, scan_tz, scan_enabled = get_schedule(d)
    next_run = None
    try:
        from .scheduler import _scheduler
        if _scheduler:
            job = _scheduler.get_job("daily_scan")
            if job:
                next_run = job.next_run_time
    except Exception:
        pass
    verified_otc = d.query_one(
        "SELECT COUNT(*) c FROM companies WHERE archived=0 AND otc_status='verified_otc'")["c"]
    return render_template("dashboard.html", new_disc=new_disc, stage_changes=stage_changes,
                           upcoming=upcoming, watch=watch, delayed=delayed,
                           last_run=last_run, failures=failures, counts=counts,
                           scan_time=scan_time, scan_tz=scan_tz, scan_enabled=scan_enabled,
                           next_run=next_run, groups=groups,
                           completed=[dict(r) for r in completed],
                           withdrawn=[dict(r) for r in withdrawn],
                           verified_otc=verified_otc)


# ---------------------------------------------------------------- candidates
def _candidate_filters():
    f = {
        "exchange": request.args.get("exchange", ""),
        "stage": request.args.get("stage", ""),
        "group": request.args.get("group", ""),
        "otc": request.args.get("otc", ""),
        "form": request.args.get("form", ""),
        "date_from": request.args.get("date_from", ""),
        "date_to": request.args.get("date_to", ""),
        "sector": request.args.get("sector", ""),
        "min_strength": request.args.get("min_strength", ""),
        "q": request.args.get("q", "").strip(),
        "sort": request.args.get("sort", "priority"),
        "dir": request.args.get("dir", "desc"),
    }
    return f


_GROUP_RANK_SQL = """CASE c.stage
    WHEN 'approval_trading_date' THEN 1
    WHEN 'conditional_approval' THEN 2
    WHEN 'application_submitted' THEN 3
    WHEN 'preparation' THEN 4
    WHEN 'intent_announced' THEN 5
    WHEN 'insufficient_evidence' THEN 6
    WHEN 'trading_commenced' THEN 7
    WHEN 'delayed_withdrawn' THEN 8
    ELSE 9 END"""


def _candidate_query(f, select="c.*", count=False):
    where = ["c.archived=0"]
    params = []
    if f["exchange"]:
        where.append("c.target_exchange=?"); params.append(f["exchange"])
    if f["stage"]:
        where.append("c.stage=?"); params.append(f["stage"])
    if f["group"]:
        from . import stages as _st
        grp = next((g for g in _st.STAGE_GROUPS if g["key"] == f["group"]), None)
        if f["group"] == "completed":
            grp = {"stages": [_st.COMPLETED_STAGE]}
        elif f["group"] == "withdrawn":
            grp = {"stages": [_st.WITHDRAWN_STAGE]}
        if grp:
            where.append(f"c.stage IN ({','.join('?' * len(grp['stages']))})")
            params.extend(grp["stages"])
    if f["otc"] == "verified":
        where.append("c.otc_status='verified_otc'")
    if f["sector"]:
        where.append("c.sector=?"); params.append(f["sector"])
    if f["min_strength"]:
        where.append("c.evidence_strength>=?"); params.append(int(f["min_strength"]))
    if f["q"]:
        where.append("(c.ticker LIKE ? OR c.name LIKE ? OR c.cik LIKE ?)")
        q = f"%{f['q']}%"
        params += [q, q, q]
    join = ""
    if f["form"] or f["date_from"] or f["date_to"]:
        join = ("JOIN (SELECT company_id, MAX(filing_date) AS fd, "
                "GROUP_CONCAT(DISTINCT form) AS forms FROM evidence e "
                "JOIN filings f ON f.id=e.filing_id GROUP BY company_id) le "
                "ON le.company_id=c.id")
        if f["form"]:
            where.append("le.forms LIKE ?"); params.append(f"%{f['form']}%")
        if f["date_from"]:
            where.append("le.fd>=?"); params.append(f["date_from"])
        if f["date_to"]:
            where.append("le.fd<=?"); params.append(f["date_to"])
    sortmap = {"strength": "c.evidence_strength", "name": "c.name",
               "ticker": "c.ticker", "stage": "c.stage",
               "checked": "c.last_checked_at", "exchange": "c.target_exchange",
               "priority": _GROUP_RANK_SQL}
    sortcol = sortmap.get(f["sort"], _GROUP_RANK_SQL)
    if f["sort"] == "priority" and f["dir"] == "desc":
        # priority: group rank ASC, then strength DESC
        sql = (f"SELECT {select} FROM companies c {join} WHERE {' AND '.join(where)} "
               f"ORDER BY {_GROUP_RANK_SQL} ASC, c.evidence_strength DESC")
    else:
        direction = "ASC" if f["dir"] == "asc" else "DESC"
        sql = (f"SELECT {select} FROM companies c {join} WHERE {' AND '.join(where)} "
               f"ORDER BY {sortcol} {direction}")
    return sql, params


@app.route("/candidates")
@login_required
def candidates():
    d = db()
    f = _candidate_filters()
    sql, params = _candidate_query(f)
    rows = d.query(sql, params)
    # attach latest evidence + quote info + checklist summary
    from . import stages as _st
    enriched = []
    for c in rows:
        c = dict(c)
        ev = d.query_one(
            """SELECT e.excerpt, f.form, f.filing_date, f.accession
               FROM evidence e JOIN filings f ON f.id=e.filing_id
               WHERE e.company_id=? AND e.is_excluded=0
               ORDER BY f.filing_date DESC LIMIT 1""", (c["id"],))
        c["latest_evidence"] = dict(ev) if ev else None
        snap = d.query_one(
            "SELECT price, volume, market_cap, quote_time, is_delayed, as_of "
            "FROM market_snapshots WHERE company_id=? ORDER BY as_of DESC LIMIT 1",
            (c["id"],))
        c["quote"] = dict(snap) if snap else None
        c["flags"] = json.loads(c["risk_flags"] or "[]")
        evall = [dict(r) for r in d.query(
            """SELECT e.*, f.form, f.filing_date, f.accession AS filing_accession
               FROM evidence e JOIN filings f ON f.id=e.filing_id
               WHERE e.company_id=? ORDER BY f.filing_date DESC""", (c["id"],))]
        items, freshness = _st.build_checklist(evall)
        c["outstanding_n"] = sum(1 for i in items if i["status"] == "outstanding")
        c["outstanding_items"] = [i for i in items if i["status"] == "outstanding"][:3]
        c["freshness_days"] = freshness
        c["group"] = _st.group_for_stage(c["stage"])
        enriched.append(c)
    sectors = [r["sector"] for r in d.query(
        "SELECT DISTINCT sector FROM companies WHERE sector IS NOT NULL ORDER BY sector")]
    return render_template("candidates.html", companies=enriched, filters=f,
                           sectors=sectors, stages=rules.STAGES,
                           stage_groups=_st.STAGE_GROUPS)


@app.route("/candidates.csv")
@login_required
def candidates_csv():
    d = db()
    f = _candidate_filters()
    sql, params = _candidate_query(
        f, select="c.ticker,c.cik,c.name,c.target_exchange,c.stage,c.evidence_strength,"
                  "c.otc_status,c.proposed_ticker,c.last_checked_at,c.sector")
    rows = d.query(sql, params)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["ticker", "cik", "name", "target_exchange", "stage",
                "evidence_strength", "otc_status", "proposed_ticker",
                "last_checked_at", "sector"])
    for r in rows:
        w.writerow([r["ticker"] or "Unknown", r["cik"], r["name"],
                    r["target_exchange"] or "Unknown",
                    rules.STAGES[r["stage"]]["label"], r["evidence_strength"],
                    r["otc_status"] or "Unknown", r["proposed_ticker"] or "Unknown",
                    r["last_checked_at"], r["sector"] or "Unknown"])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=uplist_candidates.csv"})


# ---------------------------------------------------------------- company detail
@app.route("/company/<int:cid>")
@login_required
def company(cid):
    d = db()
    c = d.query_one("SELECT * FROM companies WHERE id=?", (cid,))
    if not c:
        abort(404)
    c = dict(c)
    filings = d.query("SELECT * FROM filings WHERE cik=? ORDER BY filing_date DESC, id DESC",
                      (c["cik"],))
    evidence = d.query(
        """SELECT e.*, f.form, f.filing_date, f.accession, f.url
           FROM evidence e JOIN filings f ON f.id=e.filing_id
           WHERE e.company_id=? ORDER BY f.filing_date DESC, e.id DESC""", (cid,))
    history = d.query("SELECT * FROM stage_history WHERE company_id=? ORDER BY changed_at DESC",
                      (cid,))
    notes = d.query("""SELECT n.*, u.username FROM notes n JOIN users u ON u.id=n.user_id
                       WHERE n.company_id=? ORDER BY n.created_at DESC""", (cid,))
    snap = d.query_one("SELECT * FROM market_snapshots WHERE company_id=? "
                       "ORDER BY as_of DESC LIMIT 1", (cid,))
    facts = json.loads((dict(snap)["raw_json"] if snap else None) or "{}").get("facts") \
        if snap else None
    fin_summary = marketdata.summarize_financials(facts or {}) if facts else None
    on_watch = d.query_one("SELECT 1 FROM watchlist WHERE user_id=? AND company_id=?",
                           (session["user_id"], cid))
    # timeline: merge filings + stage changes
    timeline = []
    for f_ in filings:
        timeline.append({"date": f_["filing_date"], "kind": "filing",
                         "title": f"{f_['form']} filed", "ref": dict(f_)})
    for h in history:
        timeline.append({"date": (h["changed_at"] or "")[:10], "kind": "stage",
                         "title": f"Stage: {rules.STAGES[h['from_stage']]['label'] if h['from_stage'] else '—'} → "
                                  f"{rules.STAGES[h['to_stage']]['label']}",
                         "ref": dict(h)})
    timeline.sort(key=lambda x: x["date"] or "", reverse=True)
    from . import stages as _st
    checklist, freshness = _st.build_checklist([dict(e) for e in evidence])
    return render_template("company.html", c=c, filings=filings, evidence=evidence,
                           history=history, notes=notes, timeline=timeline,
                           snap=dict(snap) if snap else None, fin_summary=fin_summary,
                           on_watch=bool(on_watch),
                           breakdown=json.loads(c["evidence_breakdown"] or "[]"),
                           flags=json.loads(c["risk_flags"] or "[]"),
                           checklist=checklist, freshness_days=freshness,
                           next_milestone=_st.NEXT_MILESTONE.get(c["stage"], ""),
                           group=_st.group_for_stage(c["stage"]))


@app.route("/company/<int:cid>/watch", methods=["POST"])
@login_required
def toggle_watch(cid):
    d = db()
    if d.query_one("SELECT 1 FROM watchlist WHERE user_id=? AND company_id=?",
                   (session["user_id"], cid)):
        d.execute("DELETE FROM watchlist WHERE user_id=? AND company_id=?",
                  (session["user_id"], cid))
        d.audit(session["username"], "watchlist_remove", "company", str(cid))
    else:
        d.execute("INSERT INTO watchlist(user_id,company_id) VALUES(?,?)",
                  (session["user_id"], cid))
        d.audit(session["username"], "watchlist_add", "company", str(cid))
    return redirect(url_for("company", cid=cid))


@app.route("/company/<int:cid>/note", methods=["POST"])
@login_required
def add_note(cid):
    body = request.form.get("body", "").strip()
    if body:
        db().execute("INSERT INTO notes(company_id,user_id,body) VALUES(?,?,?)",
                     (cid, session["user_id"], body))
        db().audit(session["username"], "note_added", "company", str(cid),
                   body[:120])
    return redirect(url_for("company", cid=cid))


@app.route("/company/<int:cid>/refresh", methods=["POST"])
@login_required
def refresh_company(cid):
    """Re-verify OTC status, recompute stage, refresh market snapshot."""
    d = db()
    c = d.query_one("SELECT * FROM companies WHERE id=?", (cid,))
    if not c:
        abort(404)
    edgar = EdgarClient(d)
    verify_company_otc(d, edgar, dict(c))
    recompute_company(d, edgar, cid, actor=session["username"])
    try:
        marketdata.market_snapshot_for_company(d, edgar, dict(
            d.query_one("SELECT * FROM companies WHERE id=?", (cid,))))
    except Exception as e:
        flash(f"Market snapshot failed: {e}", "error")
    d.audit(session["username"], "company_refreshed", "company", str(cid))
    flash("Company re-verified and recomputed.", "ok")
    return redirect(url_for("company", cid=cid))


# ---------------------------------------------------------------- watchlist add (manual monitoring)
@app.route("/watchlist", methods=["GET", "POST"])
@login_required
def watchlist():
    d = db()
    if request.method == "POST":
        ident = request.form.get("ident", "").strip()
        if not ident:
            flash("Enter a ticker, company name, or CIK.", "error")
            return redirect(url_for("watchlist"))
        edgar = EdgarClient(d)
        cik = ""
        if ident.isdigit():
            cik = norm_cik(ident)
        elif len(ident) <= 6 and " " not in ident:
            cik = edgar.cik_for_ticker(ident) or ""
        if not cik:
            # name search via EDGAR browse
            cik = _cik_for_name(ident)
        if not cik:
            flash(f"Could not resolve '{ident}' to an SEC CIK.", "error")
            return redirect(url_for("watchlist"))
        try:
            subs = edgar.submissions(cik)
        except EdgarError as e:
            flash(f"SEC lookup failed: {e}", "error")
            return redirect(url_for("watchlist"))
        name = subs.get("name", ident)
        ticker = (subs.get("tickers") or [""])[0]
        existing = d.query_one("SELECT id FROM companies WHERE cik=?", (cik,))
        if existing:
            cid = existing["id"]
            d.execute("UPDATE companies SET source='manual' WHERE id=?", (cid,))
        else:
            cid = d.execute(
                "INSERT INTO companies(cik,ticker,name,source,current_exchange,"
                "otc_status,last_checked_at) VALUES(?,?,?,?,?,?,?)",
                (cik, ticker, name, "manual", ",".join(subs.get("exchanges") or []),
                 "verified_otc" if "OTC" in (subs.get("exchanges") or []) else "unknown",
                 utcnow()))
            verify_company_otc(d, edgar, dict(d.query_one("SELECT * FROM companies WHERE id=?",
                                                          (cid,))))
        d.execute("INSERT OR IGNORE INTO watchlist(user_id,company_id) VALUES(?,?)",
                  (session["user_id"], cid))
        d.audit(session["username"], "manual_company_added", "company", str(cid),
                f"ident={ident}")
        flash(f"Added {name} ({ticker or cik}) to your watchlist.", "ok")
        return redirect(url_for("company", cid=cid))
    rows = d.query(
        """SELECT c.* FROM watchlist w JOIN companies c ON c.id=w.company_id
           WHERE w.user_id=? ORDER BY w.added_at DESC""", (session["user_id"],))
    return render_template("watchlist.html", rows=rows)


def _cik_for_name(name):
    import requests as rq
    from bs4 import BeautifulSoup as BS
    try:
        r = rq.get("https://www.sec.gov/cgi-bin/browse-edgar",
                   params={"action": "getcompany", "company": name, "type": "",
                           "dateb": "", "owner": "include", "count": "10",
                           "action2": "getcompany"},
                   headers={"User-Agent": config.sec_user_agent(db())}, timeout=20)
        soup = BS(r.text, "lxml")
        # EDGAR returns CIK links like /cgi-bin/browse-edgar?action=getcompany&CIK=0001234567
        for a in soup.find_all("a", href=True):
            if "CIK=" in a["href"]:
                m = __import__("re").search(r"CIK=(\d+)", a["href"])
                if m:
                    return norm_cik(m.group(1))
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- reports & alerts
@app.route("/reports")
@login_required
def reports():
    d = db()
    rows = d.query("SELECT r.*, s.status AS scan_status FROM reports r "
                   "LEFT JOIN scan_runs s ON s.id=r.scan_run_id ORDER BY r.id DESC LIMIT 60")
    return render_template("reports.html", reports=rows)


@app.route("/reports/<int:rid>")
@login_required
def report_view(rid):
    d = db()
    r = d.query_one("SELECT * FROM reports WHERE id=?", (rid,))
    if not r:
        abort(404)
    html = ""
    try:
        html = open(r["html_path"]).read() if r["html_path"] else ""
    except OSError:
        pass
    return render_template("report_view.html", r=r, html=html)


@app.route("/reports/<int:rid>.pdf")
@login_required
def report_pdf(rid):
    d = db()
    r = d.query_one("SELECT * FROM reports WHERE id=?", (rid,))
    if not r or not r["pdf_path"]:
        abort(404)
    return Response(open(r["pdf_path"], "rb").read(), mimetype="application/pdf",
                    headers={"Content-Disposition":
                             f"attachment;filename=uplist_report_{r['report_date']}.pdf"})


@app.route("/reports/<int:rid>.csv")
@login_required
def report_csv(rid):
    d = db()
    r = d.query_one("SELECT * FROM reports WHERE id=?", (rid,))
    if not r or not r["csv_path"]:
        abort(404)
    return Response(open(r["csv_path"]).read(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f"attachment;filename=uplist_report_{r['report_date']}.csv"})


@app.route("/alerts")
@login_required
def alerts():
    d = db()
    rows = d.query(
        """SELECT a.*, c.ticker, c.name AS company_name FROM alerts a
           LEFT JOIN companies c ON c.id=a.company_id
           ORDER BY a.created_at DESC LIMIT 100""")
    return render_template("alerts.html", alerts=rows)


@app.route("/alerts/<int:aid>/read", methods=["POST"])
@login_required
def alert_read(aid):
    db().execute("UPDATE alerts SET is_read=1 WHERE id=?", (aid,))
    return redirect(request.referrer or url_for("alerts"))


@app.route("/alerts/read_all", methods=["POST"])
@login_required
def alerts_read_all():
    db().execute("UPDATE alerts SET is_read=1 WHERE is_read=0")
    return redirect(url_for("alerts"))


# ---------------------------------------------------------------- admin
@app.route("/admin", methods=["GET", "POST"])
@login_required
def admin():
    d = db()
    user = current_user()
    if not user["is_admin"]:
        abort(403)
    if request.method == "POST":
        action = request.form.get("action")
        actor = session["username"]
        if action == "save_schedule":
            d.set_setting("scan_time", request.form.get("scan_time", "06:00"))
            d.set_setting("scan_tz", request.form.get("scan_tz", "America/Los_Angeles"))
            d.set_setting("scan_enabled", "1" if request.form.get("scan_enabled") else "0")
            reschedule(d)
            d.audit(actor, "schedule_updated", "settings", None,
                    f"{d.get_setting('scan_time')} {d.get_setting('scan_tz')} "
                    f"enabled={d.get_setting('scan_enabled')}")
            flash("Schedule updated.", "ok")
        elif action == "save_sec":
            d.set_setting("sec_contact_email", request.form.get("sec_contact_email", ""))
            flash("SEC contact saved. Required for sustained EDGAR access.", "ok")
        elif action == "save_email":
            d.set_setting("email_enabled", "1" if request.form.get("email_enabled") else "0")
            d.set_setting("email_recipients", request.form.get("email_recipients", ""))
            d.set_setting("smtp_host", request.form.get("smtp_host", ""))
            d.set_setting("smtp_port", request.form.get("smtp_port", "587"))
            d.set_setting("smtp_user", request.form.get("smtp_user", ""))
            d.set_setting("smtp_from", request.form.get("smtp_from", ""))
            pw = request.form.get("smtp_password", "")
            if pw:
                set_secret(d, "smtp_password", pw)
            d.audit(actor, "email_settings_updated")
            flash("Email settings saved. Sending stays disabled until enabled.", "ok")
        elif action == "save_provider":
            key = request.form.get("provider_key")
            cfg_raw = request.form.get("provider_config", "")
            try:
                cfg = json.loads(cfg_raw) if cfg_raw.strip() else {}
            except json.JSONDecodeError as e:
                flash(f"Invalid JSON: {e}", "error")
                return redirect(url_for("admin"))
            api_key = request.form.get("provider_api_key", "")
            if api_key:
                set_secret(d, f"provider_{key}_api_key", api_key)
            d.execute("UPDATE providers SET config_json=?, status=? WHERE key=?",
                      (json.dumps(cfg), request.form.get("provider_status", "unknown"), key))
            d.audit(actor, "provider_updated", "provider", key)
            flash(f"Provider {key} updated.", "ok")
        return redirect(url_for("admin"))

    runs = d.query("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 20")
    providers = d.query("SELECT * FROM providers ORDER BY kind, name")
    secret_keys = list_secret_keys(d)
    audit = d.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT 50")
    rules_v = d.query("SELECT * FROM rules_versions ORDER BY name, version DESC")
    scan_time, scan_tz, scan_enabled = get_schedule(d)
    next_run = None
    try:
        from .scheduler import _scheduler
        if _scheduler:
            job = _scheduler.get_job("daily_scan")
            if job:
                next_run = job.next_run_time
    except Exception:
        pass
    settings = {r["key"]: r["value"] for r in d.query("SELECT key,value FROM settings")}
    return render_template("admin.html", runs=runs, providers=providers,
                           secret_keys=secret_keys, audit=audit, rules_v=rules_v,
                           scan_time=scan_time, scan_tz=scan_tz, scan_enabled=scan_enabled,
                           next_run=next_run, settings=settings)


def _run_scan_thread(db_path, mode):
    d = DB(db_path)
    try:
        run_scheduled_scan(d, mode=mode)
    except Exception:
        import traceback
        traceback.print_exc()


@app.route("/admin/scan_now", methods=["POST"])
@login_required
def scan_now():
    user = current_user()
    if not user["is_admin"]:
        abort(403)
    mode = request.form.get("mode", "manual")
    if mode == "initial":
        threading.Thread(target=_initial_scan_thread, args=(str(db().path),),
                         daemon=True).start()
    else:
        threading.Thread(target=_run_scan_thread, args=(str(db().path), mode),
                         daemon=True).start()
    db().audit(session["username"], "scan_triggered", None, None, f"mode={mode}")
    flash("Scan started in background. Watch Admin > Scheduler for progress.", "ok")
    return redirect(url_for("admin"))


def _initial_scan_thread(db_path):
    d = DB(db_path)
    run_scheduled_scan(d, mode="initial")


@app.route("/admin/run/<int:run_id>/retry", methods=["POST"])
@login_required
def retry_run(run_id):
    user = current_user()
    if not user["is_admin"]:
        abort(403)

    def _retry():
        d = DB(str(db().path))
        edgar = EdgarClient(d)
        runner = ScanRun(d, edgar, mode="retry", resume_run_id=run_id)
        rid, stats = runner.run()
        try:
            build_daily_report(d, rid)
        except Exception:
            pass

    threading.Thread(target=_retry, daemon=True).start()
    db().audit(session["username"], "scan_retry", "scan_run", str(run_id))
    flash(f"Retry of run #{run_id} started (resumes pending filings).", "ok")
    return redirect(url_for("admin"))


def create_app():
    db().init()
    try:
        start_scheduler(str(db().path))
    except Exception as e:
        app.logger.warning("Scheduler failed to start: %s", e)
    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host=config.BIND_HOST, port=config.BIND_PORT, debug=False)
