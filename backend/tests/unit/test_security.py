"""
Unit tests for security primitives (app/core/security.py) — Phase 16 RS256.

Covers:
  - Argon2 hash/verify round-trips
  - RS256 JWT sign/verify round-trips (conftest provisions a test keypair)
  - Expired token → TokenExpiredError
  - Wrong-type / malformed / tampered token → TokenInvalidError
  - ALGORITHM PINNING: an HS256 token (even signed with the RSA public key
    as the HMAC secret — the classic confusion attack) is rejected
  - Rollover: legacy HS256 tokens accepted read-only ONLY while
    jwt_hs256_secret is configured; flagged legacy_token=True
  - Opaque refresh token generation + SHA-256 hashing
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest

from app.core.config import get_settings
from app.core.exceptions import TokenExpiredError, TokenInvalidError
from app.core.security import (
    AlgorithmDowngradeBlockedError,
    create_access_token,
    create_refresh_token,
    decode_access_token,
    hash_password,
    hash_token,
    verify_password,
)


# ─── Password hashing ─────────────────────────────────────────────────────────

@pytest.mark.unit
def test_password_hash_verify_round_trip():
    hashed = hash_password("correct-horse-battery-staple")

    assert hashed != "correct-horse-battery-staple"
    assert hashed.startswith("$argon2")
    assert verify_password("correct-horse-battery-staple", hashed)


@pytest.mark.unit
def test_password_verify_rejects_wrong_password():
    hashed = hash_password("correct-horse-battery-staple")

    assert not verify_password("wrong-password", hashed)


@pytest.mark.unit
def test_password_hash_is_salted():
    assert hash_password("same-password") != hash_password("same-password")


@pytest.mark.unit
def test_password_verify_malformed_hash_returns_false():
    # Never raises — callers rely on a generic 401 instead of a 500
    assert not verify_password("anything", "not-a-valid-argon2-hash")


# ─── JWT access tokens (RS256) ────────────────────────────────────────────────

def _rs256_keypair() -> tuple[str, str]:
    """The PEM pair provisioned by conftest (via Settings, newline-escaped)."""
    settings = get_settings()
    return settings.jwt_private_key, settings.jwt_public_key


def _forge_hs256_token(payload: dict, secret: bytes) -> str:
    """Hand-roll an HS256 JWT (a real attacker does not use PyJWT's guards).

    PyJWT 2.10 refuses to ENCODE an HMAC token with an asymmetric key —
    raw base64url + hashlib reproduces exactly what an attacker ships.
    """
    import base64
    import hashlib
    import hmac as hmac_mod
    import json as json_mod

    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    header = _b64url(json_mod.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64url(json_mod.dumps(payload).encode())
    signing_input = f"{header}.{body}".encode()
    sig = hmac_mod.new(secret, signing_input, hashlib.sha256).digest()
    return f"{header}.{body}.{_b64url(sig)}"


@pytest.mark.unit
def test_access_token_round_trip_is_rs256():
    private_pem, _ = _rs256_keypair()
    token = create_access_token(
        "user-uuid-123",
        org_id="org-uuid-456",
        extra_claims={"roles": ["Admin"]},
    )

    # Header must declare RS256 (asymmetric signing is the Phase 16 default)
    header = pyjwt.get_unverified_header(token)
    assert header["alg"] == "RS256"

    payload = decode_access_token(token)

    assert payload["sub"] == "user-uuid-123"
    assert payload["org_id"] == "org-uuid-456"
    assert payload["type"] == "access"
    assert payload["roles"] == ["Admin"]
    assert "exp" in payload and "iat" in payload
    assert "legacy_token" not in payload


@pytest.mark.unit
def test_access_token_expiry_is_in_future():
    token = create_access_token("u", org_id="o")
    payload = decode_access_token(token)

    expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    assert expires_at > datetime.now(tz=timezone.utc)


@pytest.mark.unit
def test_expired_token_raises_token_expired():
    private_pem, _ = _rs256_keypair()
    now = datetime.now(tz=timezone.utc)
    token = pyjwt.encode(
        {
            "sub": "u",
            "org_id": "o",
            "iat": now - timedelta(hours=2),
            "exp": now - timedelta(hours=1),
            "type": "access",
        },
        private_pem,
        algorithm="RS256",
    )

    with pytest.raises(TokenExpiredError):
        decode_access_token(token)


@pytest.mark.unit
def test_wrong_type_token_raises_token_invalid():
    private_pem, _ = _rs256_keypair()
    now = datetime.now(tz=timezone.utc)
    token = pyjwt.encode(
        {
            "sub": "u",
            "org_id": "o",
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "type": "refresh",
        },
        private_pem,
        algorithm="RS256",
    )

    with pytest.raises(TokenInvalidError):
        decode_access_token(token)


@pytest.mark.unit
def test_tampered_signature_raises_token_invalid():
    token = create_access_token("u", org_id="o")
    tampered = token[:-6] + ("aaaaaa" if not token.endswith("aaaaaa") else "bbbbbb")

    with pytest.raises(TokenInvalidError):
        decode_access_token(tampered)


@pytest.mark.unit
def test_wrong_key_raises_token_invalid():
    """Signed with a DIFFERENT RSA key — signature verification must fail."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_private_pem = other_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")

    now = datetime.now(tz=timezone.utc)
    token = pyjwt.encode(
        {"sub": "u", "org_id": "o", "iat": now,
         "exp": now + timedelta(minutes=5), "type": "access"},
        other_private_pem,
        algorithm="RS256",
    )

    with pytest.raises(TokenInvalidError):
        decode_access_token(token)


@pytest.mark.unit
def test_algorithm_confusion_blocked():
    """Phase 16 INVARIANT: an HS256 token — even signed with the RS256
    PUBLIC key as the HMAC secret — is rejected, never accepted."""
    _, public_pem = _rs256_keypair()
    now = int(datetime.now(tz=timezone.utc).timestamp())
    forged = _forge_hs256_token(
        {"sub": "u", "org_id": "o", "iat": now,
         "exp": now + 300, "type": "access"},
        public_pem.encode(),  # attacker uses the public key as the HMAC secret
    )

    with pytest.raises(TokenInvalidError):
        decode_access_token(forged)


@pytest.mark.unit
def test_algorithm_confusion_raises_specific_error_type():
    """The confusion attempt surfaces as AlgorithmDowngradeBlockedError so
    the auth layer can write the audit event (still a TokenInvalidError)."""
    _, public_pem = _rs256_keypair()
    now = int(datetime.now(tz=timezone.utc).timestamp())
    forged = _forge_hs256_token(
        {"sub": "u", "org_id": "o", "iat": now,
         "exp": now + 300, "type": "access"},
        public_pem.encode(),
    )

    with pytest.raises(AlgorithmDowngradeBlockedError):
        decode_access_token(forged)


@pytest.mark.unit
def test_rollover_accepts_legacy_hs256_token_readonly(monkeypatch):
    """During an explicit rollover window, legacy HS256 tokens verify but
    are flagged legacy_token=True (force refresh downstream)."""
    monkeypatch.setenv("JWT_HS256_SECRET", "legacy-hs256-secret-for-rollover")
    get_settings.cache_clear()
    try:
        now = datetime.now(tz=timezone.utc)
        legacy = pyjwt.encode(
            {"sub": "u", "org_id": "o", "iat": now,
             "exp": now + timedelta(minutes=5), "type": "access"},
            "legacy-hs256-secret-for-rollover",
            algorithm="HS256",
        )
        payload = decode_access_token(legacy)
        assert payload["sub"] == "u"
        assert payload["legacy_token"] is True
    finally:
        get_settings.cache_clear()


@pytest.mark.unit
def test_rollover_rejects_wrong_hs256_secret(monkeypatch):
    """Rollover active, but the HS256 token was signed with an unknown
    secret — rejected (no fallback to the current signing secret)."""
    monkeypatch.setenv("JWT_HS256_SECRET", "legacy-hs256-secret-for-rollover")
    get_settings.cache_clear()
    try:
        now = datetime.now(tz=timezone.utc)
        forged = pyjwt.encode(
            {"sub": "u", "org_id": "o", "iat": now,
             "exp": now + timedelta(minutes=5), "type": "access"},
            "a-completely-different-secret-key",
            algorithm="HS256",
        )
        with pytest.raises(TokenInvalidError):
            decode_access_token(forged)
    finally:
        get_settings.cache_clear()


@pytest.mark.unit
def test_hs256_token_rejected_without_rollover(monkeypatch):
    """No rollover secret configured: ANY HS256 token is rejected."""
    monkeypatch.delenv("JWT_HS256_SECRET", raising=False)
    get_settings.cache_clear()
    try:
        now = datetime.now(tz=timezone.utc)
        token = pyjwt.encode(
            {"sub": "u", "org_id": "o", "iat": now,
             "exp": now + timedelta(minutes=5), "type": "access"},
            "any-secret",
            algorithm="HS256",
        )
        with pytest.raises(TokenInvalidError):
            decode_access_token(token)
    finally:
        get_settings.cache_clear()


@pytest.mark.unit
def test_malformed_token_raises_token_invalid():
    with pytest.raises(TokenInvalidError):
        decode_access_token("not-a-jwt-at-all")


# ─── Opaque refresh tokens ────────────────────────────────────────────────────

@pytest.mark.unit
def test_refresh_tokens_are_unique_and_long():
    tokens = {create_refresh_token() for _ in range(50)}

    assert len(tokens) == 50
    assert all(len(t) >= 48 for t in tokens)


@pytest.mark.unit
def test_hash_token_is_deterministic_sha256():
    raw = create_refresh_token()
    digest = hash_token(raw)

    assert digest == hash_token(raw)
    assert len(digest) == 64  # SHA-256 hex
    assert raw not in digest
