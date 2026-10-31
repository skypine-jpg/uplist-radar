"""SQLite access layer: schema init, row helpers, settings."""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

_local = threading.local()


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DB:
    def __init__(self, path=None):
        self.path = Path(path or config.DB_PATH)

    def connect(self):
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def init(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        sql = SCHEMA_PATH.read_text()
        with self.connect() as conn:
            conn.executescript(sql)
            conn.commit()
        self._migrate()
        self._seed()

    def _migrate(self):
        """Add columns introduced after the initial schema."""
        want = {
            "scan_queue": [("hit_doc", "TEXT")],
            "filings": [("extra_docs", "TEXT")],
            "scan_runs": [("filings_failed", "INTEGER DEFAULT 0"),
                          ("filings_excluded", "INTEGER DEFAULT 0")],
        }
        with self.connect() as conn:
            for table, cols in want.items():
                existing = {r["name"] for r in
                            conn.execute(f"PRAGMA table_info({table})").fetchall()}
                for name, ctype in cols:
                    if name not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ctype}")
            conn.commit()

    def _seed(self):
        with self.connect() as conn:
            cur = conn.cursor()
            # Providers
            providers = [
                ("edgar", "SEC EDGAR", "filings", "ok",
                 {"base": "https://www.sec.gov", "fts": "https://efts.sec.gov",
                  "note": "Primary filing source. Free; requires identifying User-Agent."}),
                ("stooq", "Stooq (best-effort quotes)", "market_data", "degraded",
                 {"note": "Free, no key. Spotty OTC coverage; quotes delayed/unknown. "
                          "Configure a licensed provider (OTC Markets, Polygon, Nasdaq Data Link) "
                          "for production market data."}),
                ("companyfacts", "SEC Company Facts (XBRL)", "market_data", "ok",
                 {"note": "Free. Shares outstanding / balance-sheet facts from filed XBRL."}),
                ("openai", "AI enrichment (optional)", "ai", "not_configured",
                 {"note": "Optional LLM for summaries/context. Disabled until an API key is stored."}),
            ]
            for key, name, kind, status, cfg in providers:
                cur.execute(
                    "INSERT OR IGNORE INTO providers(key,name,kind,status,config_json) VALUES(?,?,?,?,?)",
                    (key, name, kind, status, json.dumps(cfg)))
            # Settings
            defaults = {
                "scan_time": config.DEFAULT_SCAN_TIME,
                "scan_tz": config.DEFAULT_SCAN_TZ,
                "scan_enabled": "1",
                "scan_overlap_days": str(config.SCAN_OVERLAP_DAYS),
                "sec_contact_email": config.SEC_CONTACT_EMAIL,
                "email_enabled": "0",
                "email_recipients": "",
                "smtp_host": "", "smtp_port": "587", "smtp_user": "",
                "smtp_from": "",
                "initial_window_months": "12",
            }
            for k, v in defaults.items():
                cur.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
            # Rules versions
            from . import rules  # noqa: deferred import to avoid cycle at module import
            for name, version, content in rules.all_rules():
                cur.execute(
                    "INSERT OR IGNORE INTO rules_versions(name,version,content_json,active) VALUES(?,?,?,?)",
                    (name, version, json.dumps(content), 1))
            conn.commit()

    # ---- generic helpers ----
    def query(self, sql, params=()):
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        with self.connect() as conn:
            return conn.execute(sql, params).fetchone()

    def execute(self, sql, params=()):
        with self.connect() as conn:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.lastrowid

    def executemany(self, sql, params):
        with self.connect() as conn:
            conn.executemany(sql, params)
            conn.commit()

    # ---- settings ----
    def get_setting(self, key, default=""):
        row = self.query_one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key, value):
        self.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, utcnow()))

    # ---- audit ----
    def audit(self, actor, action, entity=None, entity_id=None, detail=None):
        self.execute(
            "INSERT INTO audit_log(actor,action,entity,entity_id,detail) VALUES(?,?,?,?,?)",
            (actor, action, entity, entity_id, detail))
