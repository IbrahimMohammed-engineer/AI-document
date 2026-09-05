"""
JWT issuance / verification and password hashing utilities.

This module is intentionally minimal for Phase 0/1 — it provides the
security primitives that Phase 2 (AuthService) will build on top of.
No business logic lives here, only cryptographic operations.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

from app.core.config import get_settings
from app.core.exceptions import TokenExpiredError, TokenInvalidError

logger = logging.getLogger(__name__)

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

# Algorithm pinning (Phase 16 plan §4.3 — NON-NEGOTIABLE): RS256 tokens are
# verified with EXACTLY ["RS256"], never ["RS256", "HS256"]. Offering both
# enables the algorithm-confusion attack (an HS256 token forged with the
# RSA public key as the HMAC secret would verify).
_RS256_ONLY = ["RS256"]
_HS256_ONLY = ["HS256"]


class AlgorithmDowngradeBlockedError(TokenInvalidError):
    """A token presented with a non-pinned algorithm was rejected.

    Subclass of TokenInvalidError so every existing handler keeps returning
    a generic 401 — but the auth layer can detect the specific attack and
    write the JWT_ALGORITHM_DOWNGRADE_BLOCKED audit event (Backend §54).
    """

    error_code = "TOKEN_INVALID"


def _signing_key_and_algorithm() -> tuple[str, str]:
    """Resolve the signing key + algorithm.

    RS256 (default): sign with jwt_private_key. When no RSA keypair is
    configured (local dev/tests without keys), fall back to HS256 with the
    legacy secret and log once — production validates the keypair at
    settings load (config.validate_jwt_key_config).
    """
    s = get_settings()
    if s.jwt_algorithm == "RS256" and s.jwt_private_key:
        return s.jwt_private_key, "RS256"
    return s.jwt_secret_key, "HS256"


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
        A signed JWT string (RS256 when a keypair is configured).
    """
    now = datetime.now(tz=timezone.utc)
    expire = now + timedelta(minutes=get_settings().jwt_access_token_expire_minutes)

    payload: dict[str, Any] = {
        "sub": subject,
        "org_id": org_id,
        "iat": now,
        "exp": expire,
        "type": "access",
    }
    if extra_claims:
        payload.update(extra_claims)

    key, algorithm = _signing_key_and_algorithm()
    return jwt.encode(payload, key, algorithm=algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and verify a JWT access token.

    RS256 is verified with the PUBLIC key, algorithm PINNED to exactly
    ["RS256"]. The token's declared header algorithm is dispatched FIRST, so
    an HS256 token is only ever routed to the read-only rollover verifier
    (and rejected outright when no rollover window is configured) — never
    offered to the RS256 path. During the window, legacy tokens are flagged
    ``legacy_token=True`` so callers can force a refresh.

    Raises:
        TokenExpiredError: if the token's `exp` has passed.
        TokenInvalidError: if the signature is wrong, the token is malformed,
            or the `type` claim is not `access`.
        AlgorithmDowngradeBlockedError: if an HS256/unknown-alg token arrives
            while RS256 is pinned and no rollover window is open
            (algorithm-confusion attempt — audited by the auth layer).
    """
    s = get_settings()

    def _check_type(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("type") != "access":
            raise TokenInvalidError("Token type is not 'access'.")
        return payload

    def _reject_downgrade() -> None:
        logger.warning(
            "JWT rejected: non-RS256 algorithm presented against RS256-pinned "
            "verification (attempted HS256)."
        )
        raise AlgorithmDowngradeBlockedError("Access token is invalid.")

    try:
        header_alg = jwt.get_unverified_header(token).get("alg")
    except jwt.PyJWTError:
        raise TokenInvalidError("Access token is invalid.")

    if s.jwt_algorithm == "RS256" and s.jwt_public_key:
        if header_alg == "RS256":
            try:
                payload = jwt.decode(token, s.jwt_public_key, algorithms=_RS256_ONLY)
            except jwt.ExpiredSignatureError:
                raise TokenExpiredError("Access token has expired. Please refresh.")
            except jwt.PyJWTError:
                raise TokenInvalidError("Access token is invalid.")
            return _check_type(payload)

        if header_alg == "HS256" and s.jwt_hs256_secret:
            # ── Rollover grace period: legacy HS256 tokens (read-only) ────
            try:
                legacy = jwt.decode(token, s.jwt_hs256_secret, algorithms=_HS256_ONLY)
            except jwt.ExpiredSignatureError:
                raise TokenExpiredError("Access token has expired. Please refresh.")
            except jwt.PyJWTError:
                raise TokenInvalidError("Access token is invalid.")
            logger.info("Legacy HS256 token accepted during rollover window.")
            return _check_type({**legacy, "legacy_token": True})

        # HS256 without a rollover window, or any other algorithm
        _reject_downgrade()

    # ── Explicit HS256 mode / no-keypair dev fallback ─────────────────────
    if header_alg != "HS256":
        raise TokenInvalidError("Access token is invalid.")
    try:
        payload = jwt.decode(token, s.jwt_secret_key, algorithms=_HS256_ONLY)
    except jwt.ExpiredSignatureError:
        raise TokenExpiredError("Access token has expired. Please refresh.")
    except jwt.PyJWTError:
        raise TokenInvalidError("Access token is invalid.")

    return _check_type(payload)


def create_refresh_token() -> str:
    """Return a cryptographically secure opaque refresh token."""
    return secrets.token_urlsafe(48)


def hash_token(raw_token: str) -> str:
    """Return a SHA-256 hex digest for a token stored server-side."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
