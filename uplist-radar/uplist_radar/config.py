"""Configuration: env vars with sane defaults + DB-backed settings overrides."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("UPLIST_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
FILINGS_DIR = DATA_DIR / "filings"
FILINGS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR = Path(os.environ.get("UPLIST_LOGS_DIR", BASE_DIR / "logs"))
LOGS_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR = DATA_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.environ.get("UPLIST_DB", DATA_DIR / "uplist_radar.db"))

# SEC requires a User-Agent identifying the tool + contact. MUST be set to a
# real contact email for sustained use (Settings page or env var).
SEC_CONTACT_EMAIL = os.environ.get("UPLIST_SEC_CONTACT", "")
APP_NAME = "UplistRadar"
APP_VERSION = "1.0.0"


def sec_user_agent(db=None):
    email = SEC_CONTACT_EMAIL
    if db is not None:
        try:
            email = db.get_setting("sec_contact_email", email) or email
        except Exception:
            pass
    if not email:
        email = "unset-contact@example.invalid"
    return f"{APP_NAME}/{APP_VERSION} (contact: {email})"


# Scheduler defaults (editable in Admin UI; stored in settings table)
DEFAULT_SCAN_TIME = os.environ.get("UPLIST_SCAN_TIME", "06:00")  # HH:MM
DEFAULT_SCAN_TZ = os.environ.get("UPLIST_SCAN_TZ", "America/Los_Angeles")
SCAN_OVERLAP_DAYS = int(os.environ.get("UPLIST_SCAN_OVERLAP_DAYS", "7"))

# Politeness: SEC allows up to 10 req/s; we stay well under.
SEC_MIN_INTERVAL = float(os.environ.get("UPLIST_SEC_MIN_INTERVAL", "0.4"))
SEC_MAX_RETRIES = int(os.environ.get("UPLIST_SEC_MAX_RETRIES", "5"))

FLASK_SECRET_KEY = os.environ.get("UPLIST_FLASK_SECRET", "")
MASTER_KEY_ENV = "UPLIST_MASTER_KEY"  # env var holding fernet key for secrets

BIND_HOST = os.environ.get("UPLIST_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("UPLIST_PORT", "8765"))
