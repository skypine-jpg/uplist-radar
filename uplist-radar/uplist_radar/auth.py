"""Password auth: pbkdf2_hmac (stdlib), single- or multi-user."""
import hashlib
import hmac
import os

ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2${ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, iters, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def create_user(db, username, password, is_admin=False):
    if db.query_one("SELECT id FROM users WHERE username=?", (username,)):
        raise ValueError("user already exists")
    uid = db.execute("INSERT INTO users(username,password_hash,is_admin) VALUES(?,?,?)",
                     (username, hash_password(password), 1 if is_admin else 0))
    db.audit("system", "user_created", "user", str(uid), f"username={username}")
    return uid


def authenticate(db, username, password):
    row = db.query_one("SELECT * FROM users WHERE username=?", (username,))
    if row and verify_password(password, row["password_hash"]):
        return dict(row)
    return None
