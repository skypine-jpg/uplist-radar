#!/usr/bin/env python3
"""Standalone scan entrypoint for cron/systemd (no web server needed).

Usage:
    scripts/scan_now.py [--mode initial|scheduled|manual] [--from YYYY-MM-DD] [--to YYYY-MM-DD]

Runs the full pipeline (discovery -> extraction -> verification -> change
detection -> report) against the SQLite DB and exits non-zero on fatal failure.
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from uplist_radar.db import DB
from uplist_radar.scheduler import run_scheduled_scan

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="scheduled",
                    choices=["initial", "scheduled", "manual"])
    ap.add_argument("--from", dest="from_date", default=None)
    ap.add_argument("--to", dest="to_date", default=None)
    args = ap.parse_args()

    db = DB()
    db.init()
    run_id, report_id, stats = run_scheduled_scan(db, mode=args.mode)
    print(f"run_id={run_id} report_id={report_id} stats={stats}")
    if stats.get("errors") and not stats.get("fetched"):
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
