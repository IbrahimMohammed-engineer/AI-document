"""
JWT issuance / verification and password hashing utilities.

This module is intentionally minimal for Phase 0/1 — it provides the
security primitives that Phase 2 (AuthService) will build on top of.
No business logic lives here, only cryptographic operations.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

from app.core.config import get_settings
from app.core.exceptions import TokenExpiredError, TokenInvalidError

settings = get_settings()

# ─── Password hashing (Argon2) ────────────────────────────────────────────────

_hasher = PasswordHasher(
    time_cost=2,       # number of iterations
    memory_cost=65536, # 64 MB
    parallelism=2,
    hash_len=32,
    salt_len=16,
)


def hash_password(plain_password: str) -> str:
    """Hash a plaintext password with Argon2id.

    The returned string includes the algorithm parameters and salt —
    store this entire string in the database.
    """
    return _hasher.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Return True if the plaintext matches the stored hash.

    Always returns False (never raises) on mismatch, so callers can
    use a generic 401 without distinguishing error types.
    """
    try:
        return _hasher.verify(hashed_password, plain_password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# ─── JWT (access tokens) ──────────────────────────────────────────────────────

def create_access_token(
    subject: str,
    *,
    org_id: str,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Issue a short-lived JWT access token.

    Args:
        subject: The user's UUID (becomes the JWT `sub` claim).
        org_id: The user's organization UUID (included as `org_id` claim).
        extra_claims: Optional additional claims merged into the payload
            (e.g., advisory `roles` snapshot for UI use only).

    Returns:
        A signed JWT string.
    """
    now = datetime.now(tz=timezone.utc)
    expire = now + timedelta(minutes=settings.jwt_access_token_expire_minutes)

    payload: dict[str, Any] = {
        "sub": subject,
        "org_id": org_id,
        "iat": now,
        "exp": expire,
        "type": "access",
    }
    if extra_claims:
        payload.update(extra_claims)

    return jwt.encode(
        payload,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and verify a JWT access token.

    Raises:
        TokenExpiredError: if the token's `exp` has passed.
        TokenInvalidError: if the signature is wrong, the token is malformed,
            or the `type` claim is not `access`.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError:
        raise TokenExpiredError("Access token has expired. Please refresh.")
    except jwt.PyJWTError:
        raise TokenInvalidError("Access token is invalid.")

    if payload.get("type") != "access":
        raise TokenInvalidError("Token type is not 'access'.")

    return payload


def create_refresh_token() -> str:
    """Return a cryptographically secure opaque refresh token."""
    return secrets.token_urlsafe(48)


def hash_token(raw_token: str) -> str:
    """Return a SHA-256 hex digest for a token stored server-side."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
