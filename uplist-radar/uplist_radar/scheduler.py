"""Server-side scheduler: APScheduler daily job at 6:00 America/Los_Angeles.

The job runs inside the app process (or via scripts/scan_now.py from cron/
systemd). DST is handled by the IANA timezone. The editable schedule lives in
the settings table (Admin UI); the in-process scheduler re-reads it every run
and reschedules itself when changed.
"""
import logging
import smtplib
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import config
from .db import DB, utcnow
from .edgar import EdgarClient
from .pipeline import ScanRun
from .reports import build_daily_report
from .secrets import get_secret

log = logging.getLogger("uplist_radar.scheduler")
_scheduler = None


def get_schedule(db):
    return (db.get_setting("scan_time", config.DEFAULT_SCAN_TIME),
            db.get_setting("scan_tz", config.DEFAULT_SCAN_TZ),
            db.get_setting("scan_enabled", "1") == "1")


def run_scheduled_scan(db=None, mode="scheduled"):
    """Execute one scan + report. Safe to call from any scheduler."""
    db = db or DB()
    log.info("Starting %s scan", mode)
    edgar = EdgarClient(db)
    runner = ScanRun(db, edgar, mode=mode)
    run_id, stats = runner.run()
    report_id = None
    try:
        report_id = build_daily_report(db, run_id)
        log.info("Report %s built for run %s", report_id, run_id)
    except Exception as e:
        log.exception("Report build failed for run %s: %s", run_id, e)
        db.execute("INSERT INTO alerts(alert_type,title,body) VALUES("
                   "'scan_failure','Report build failed',?)",
                   (f"Run #{run_id}: {e}",))
    maybe_send_email(db, run_id, report_id)
    return run_id, report_id, stats


def maybe_send_email(db, run_id, report_id):
    """Optional email delivery. Disabled until configured (email_enabled=1)."""
    if db.get_setting("email_enabled", "0") != "1":
        return False
    recipients = [r.strip() for r in db.get_setting("email_recipients", "").split(",")
                  if r.strip()]
    if not recipients:
        log.warning("Email enabled but no recipients configured")
        return False
    host = db.get_setting("smtp_host", "")
    port = int(db.get_setting("smtp_port", "587") or 587)
    user = db.get_setting("smtp_user", "")
    sender = db.get_setting("smtp_from", user)
    password = get_secret(db, "smtp_password")
    if not (host and user and password):
        log.warning("Email enabled but SMTP settings incomplete")
        return False
    rep = db.query_one("SELECT * FROM reports WHERE id=?", (report_id,)) if report_id else None
    msg = EmailMessage()
    msg["Subject"] = f"[Uplist Radar] Daily report — {rep['report_date'] if rep else 'n/a'}"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content((rep["markdown"] if rep else "Report unavailable."))
    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
        db.audit("system", "email_sent", "report", str(report_id),
                 f"recipients={len(recipients)}")
        return True
    except Exception as e:
        log.exception("Email send failed: %s", e)
        db.execute("INSERT INTO alerts(alert_type,title,body) VALUES("
                   "'scan_failure','Email delivery failed',?)", (str(e)[:500],))
        return False


def _job(db_path):
    db = DB(db_path)
    try:
        run_scheduled_scan(db, mode="scheduled")
    finally:
        # re-read schedule in case the admin changed it
        reschedule(db)


def reschedule(db=None):
    global _scheduler
    db = db or DB()
    if _scheduler is None:
        return
    scan_time, tz_name, enabled = get_schedule(db)
    _scheduler.remove_all_jobs()
    if enabled:
        hh, mm = (scan_time.split(":") + ["0"])[:2]
        trig = CronTrigger(hour=int(hh), minute=int(mm),
                           timezone=ZoneInfo(tz_name))
        _scheduler.add_job(_job, trig, args=[str(db.path)], id="daily_scan",
                           replace_existing=True, misfire_grace_time=3600,
                           coalesce=True, max_instances=1)
        log.info("Daily scan scheduled at %s %s (enabled)", scan_time, tz_name)
    else:
        log.info("Daily scan disabled")


def start(db_path=None):
    """Start the in-process background scheduler. Returns the scheduler."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler(timezone=ZoneInfo(config.DEFAULT_SCAN_TZ))
    _scheduler.start()
    reschedule(DB(db_path) if db_path else DB())
    return _scheduler
