"""Server-side secret storage: Fernet-encrypted values in the `secrets` table.

The master key comes from the UPLIST_MASTER_KEY env var when set (recommended
for production); otherwise a key file under the data dir is generated once.
The key file is chmod 600. Never log secret values.
"""
import base64
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from . import config


def _master_key() -> bytes:
    env = os.environ.get(config.MASTER_KEY_ENV)
    if env:
        raw = env.encode()
        # Accept a urlsafe-b64 Fernet key or derive from arbitrary string.
        try:
            base64.urlsafe_b64decode(raw)
            if len(raw) == 44:
                return raw
        except Exception:
            pass
        import hashlib
        return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    key_path = config.DATA_DIR / ".master_key"
    if key_path.exists():
        return key_path.read_bytes().strip()
    key = Fernet.generate_key()
    key_path.write_bytes(key + b"\n")
    os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
    return key


_fernet = None


def fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_master_key())
    return _fernet


def set_secret(db, key: str, value: str):
    token = fernet().encrypt(value.encode()).decode()
    db.execute(
        "INSERT INTO secrets(key,ciphertext) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET ciphertext=excluded.ciphertext, "
        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
        (key, token))


def get_secret(db, key: str):
    row = db.query_one("SELECT ciphertext FROM secrets WHERE key=?", (key,))
    if not row:
        return None
    try:
        return fernet().decrypt(row["ciphertext"].encode()).decode()
    except InvalidToken:
        return None


def delete_secret(db, key: str):
    db.execute("DELETE FROM secrets WHERE key=?", (key,))


def list_secret_keys(db):
    return [r["key"] for r in db.query("SELECT key, updated_at FROM secrets ORDER BY key")]
