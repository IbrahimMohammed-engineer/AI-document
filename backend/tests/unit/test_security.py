"""
Unit tests for security primitives (app/core/security.py).

Covers:
  - Argon2 hash/verify round-trips
  - JWT sign/verify round-trips
  - Expired token → TokenExpiredError
  - Wrong-type / malformed / tampered token → TokenInvalidError
  - Opaque refresh token generation + SHA-256 hashing
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest

from app.core.config import get_settings
from app.core.exceptions import TokenExpiredError, TokenInvalidError
from app.core.security import (
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


# ─── JWT access tokens ────────────────────────────────────────────────────────

@pytest.mark.unit
def test_access_token_round_trip():
    token = create_access_token(
        "user-uuid-123",
        org_id="org-uuid-456",
        extra_claims={"roles": ["Admin"]},
    )

    payload = decode_access_token(token)

    assert payload["sub"] == "user-uuid-123"
    assert payload["org_id"] == "org-uuid-456"
    assert payload["type"] == "access"
    assert payload["roles"] == ["Admin"]
    assert "exp" in payload and "iat" in payload


@pytest.mark.unit
def test_access_token_expiry_is_in_future():
    token = create_access_token("u", org_id="o")
    payload = decode_access_token(token)

    expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    assert expires_at > datetime.now(tz=timezone.utc)


@pytest.mark.unit
def test_expired_token_raises_token_expired():
    settings = get_settings()
    now = datetime.now(tz=timezone.utc)
    token = pyjwt.encode(
        {
            "sub": "u",
            "org_id": "o",
            "iat": now - timedelta(hours=2),
            "exp": now - timedelta(hours=1),
            "type": "access",
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(TokenExpiredError):
        decode_access_token(token)


@pytest.mark.unit
def test_wrong_type_token_raises_token_invalid():
    settings = get_settings()
    now = datetime.now(tz=timezone.utc)
    token = pyjwt.encode(
        {
            "sub": "u",
            "org_id": "o",
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "type": "refresh",
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
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
def test_wrong_secret_raises_token_invalid():
    now = datetime.now(tz=timezone.utc)
    token = pyjwt.encode(
        {"sub": "u", "org_id": "o", "iat": now, "exp": now + timedelta(minutes=5), "type": "access"},
        "a-completely-different-secret-key",
        algorithm="HS256",
    )

    with pytest.raises(TokenInvalidError):
        decode_access_token(token)


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
