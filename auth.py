"""
auth.py — Step 8: authentication for the CMPDI/CIL platform.

Design:
- Passwords are hashed with bcrypt (per-password random salt, industry
  standard). Plaintext passwords are never stored, logged, or returned.
- Sessions are opaque random server-side tokens (secrets.token_urlsafe),
  stored in a `sessions` table with an expiry — NOT a JWT. This means
  logout / expiration are real, immediate, and enforced server-side by
  deleting the row, rather than relying on a client trusting an unexpired
  signed token.
- The token lives in an HttpOnly, SameSite=Lax cookie, so it is never
  readable by frontend JavaScript and is not attached to cross-site
  requests. Secure is set dynamically based on the actual request scheme
  (True over https, False over local http) so it works in both a TLS
  deployment and local development without a config flag to forget.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt

SESSION_DURATION_HOURS = 24
SESSION_COOKIE_NAME = "session_token"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(ValueError):
    """User-safe validation/auth error message."""


def validate_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not email or not _EMAIL_RE.match(email):
        raise AuthError("Please enter a valid email address.")
    return email


def validate_password(password: str) -> str:
    if not password or len(password) < 8:
        raise AuthError("Password must be at least 8 characters long.")
    if len(password) > 200:
        raise AuthError("Password is too long.")
    return password


def hash_password(password: str) -> str:
    """Returns a bcrypt hash (includes its own random salt) as a UTF-8 string."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        # Malformed hash, e.g. from data corruption — never crash the login flow.
        return False


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def session_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=SESSION_DURATION_HOURS)


def is_expired(expires_at) -> bool:
    """
    Robust to either a real datetime (the normal case with psycopg2/Postgres)
    or an ISO-format string (e.g. some SQLite test harnesses / drivers),
    so a driver-level type quirk can never accidentally treat an expired
    session as valid.
    """
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= expires_at
