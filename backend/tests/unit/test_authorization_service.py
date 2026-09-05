"""
Unit tests — single document/version authorization (Phase 12, plan §9.2).

Covers:
  - _check_document_access_level: the extracted access-level rule
    (organization / restricted / private / unknown × owner / non-owner)
  - authorize_document_version: the per-version authorizer used by the
    comparison API — against a lightweight fake AsyncSession (no DB).

Authorization matrix (§16.4 — creation-time half; the API-level matrix is
covered in tests/api/test_compare.py):
  - both sides authorized → allowed
  - either side unauthorized → NotFoundError (never 403 — no existence leak)
  - cross-org / missing / soft-deleted → NotFoundError
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.core.exceptions import NotFoundError
from app.services.authorization_service import AuthorizationService


# ── Stubs ─────────────────────────────────────────────────────────────────────

def _user(user_id: str = "user-1", org_id: str = "org-1") -> SimpleNamespace:
    return SimpleNamespace(id=user_id, organization_id=org_id)


def _document(
    *,
    doc_id: str = "doc-1",
    org_id: str = "org-1",
    owner_id: str = "user-1",
    access_level: str = "organization",
    deleted_at: Any = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=doc_id,
        organization_id=org_id,
        owner_id=owner_id,
        access_level=access_level,
        deleted_at=deleted_at,
    )


def _version(version_id: str = "ver-1", document_id: str = "doc-1") -> SimpleNamespace:
    return SimpleNamespace(id=version_id, document_id=document_id, status="READY")


class _FakeResult:
    """Mimics the one method authorize_document_version uses per query."""

    def __init__(self, value: Any):
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FakeSession:
    """Returns queued results in order (version lookup, then document lookup)."""

    def __init__(self, version: Any, document: Any):
        self._results = [_FakeResult(version), _FakeResult(document)]
        self.executed: list[Any] = []

    async def execute(self, stmt: Any) -> _FakeResult:
        self.executed.append(stmt)
        return self._results.pop(0)


# ── _check_document_access_level ──────────────────────────────────────────────

def _grant_ids(ids: list[str]):
    """Fake execute() result for the Phase 16 grant lookup (rows of doc ids)."""
    class _R:
        def all(self):
            return [(i,) for i in ids]

    return _R()


class _GrantSession:
    """Async session stub whose execute() returns the given grant rows."""

    def __init__(self, granted: list[str]):
        self._granted = granted

    async def execute(self, stmt: Any) -> Any:
        return _grant_ids(self._granted)


@pytest.mark.unit
class TestCheckDocumentAccessLevel:

    @pytest.mark.asyncio
    async def test_organization_any_member(self):
        doc = _document(access_level="organization", owner_id="someone-else")
        assert await AuthorizationService._check_document_access_level(
            doc, _user(), _GrantSession([])
        ) is True

    @pytest.mark.asyncio
    async def test_private_owner_allowed(self):
        doc = _document(access_level="private", owner_id="user-1")
        assert await AuthorizationService._check_document_access_level(
            doc, _user(), _GrantSession([])
        ) is True

    @pytest.mark.asyncio
    async def test_private_non_owner_denied(self):
        doc = _document(access_level="private", owner_id="owner-2")
        assert await AuthorizationService._check_document_access_level(
            doc, _user(), _GrantSession([])
        ) is False

    @pytest.mark.asyncio
    async def test_restricted_owner_always_allowed(self):
        """Phase 16: the owner bypasses the grant table entirely."""
        doc = _document(access_level="restricted", owner_id="user-1")
        assert await AuthorizationService._check_document_access_level(
            doc, _user(), _GrantSession([])
        ) is True

    @pytest.mark.asyncio
    async def test_restricted_non_owner_with_live_grant(self):
        """Phase 16: a live document_permissions grant confers access."""
        doc = _document(access_level="restricted", owner_id="someone-else")
        assert await AuthorizationService._check_document_access_level(
            doc, _user(), _GrantSession(["doc-1"])
        ) is True

    @pytest.mark.asyncio
    async def test_restricted_non_owner_without_grant_denied(self):
        """Phase 16: RESTRICTED defaults to DENY for non-owners."""
        doc = _document(access_level="restricted", owner_id="someone-else")
        assert await AuthorizationService._check_document_access_level(
            doc, _user(), _GrantSession([])
        ) is False

    @pytest.mark.asyncio
    async def test_unknown_access_level_denied(self):
        doc = _document(access_level="top-secret")
        assert await AuthorizationService._check_document_access_level(
            doc, _user(), _GrantSession([])
        ) is False


# ── authorize_document_version ────────────────────────────────────────────────

@pytest.mark.unit
class TestAuthorizeDocumentVersion:

    @pytest.mark.asyncio
    async def test_success_returns_version_and_document(self):
        version, document = _version(), _document()
        session = _FakeSession(version, document)
        out_version, out_document = await AuthorizationService.authorize_document_version(
            _user(), "ver-1", session  # type: ignore[arg-type]
        )
        assert out_version is version
        assert out_document is document

    @pytest.mark.asyncio
    async def test_missing_version_raises_not_found(self):
        session = _FakeSession(None, None)
        with pytest.raises(NotFoundError):
            await AuthorizationService.authorize_document_version(
                _user(), "missing", session  # type: ignore[arg-type]
            )

    @pytest.mark.asyncio
    async def test_cross_org_version_is_404_not_403(self):
        """Existence-leakage rule: a cross-org version looks exactly like a
        missing one (NotFoundError, never ForbiddenError)."""
        version = _version()
        document = _document(org_id="org-OTHER")
        session = _FakeSession(version, document)
        with pytest.raises(NotFoundError):
            await AuthorizationService.authorize_document_version(
                _user(org_id="org-1"), "ver-1", session  # type: ignore[arg-type]
            )

    @pytest.mark.asyncio
    async def test_missing_document_raises_not_found(self):
        session = _FakeSession(_version(), None)
        with pytest.raises(NotFoundError):
            await AuthorizationService.authorize_document_version(
                _user(), "ver-1", session  # type: ignore[arg-type]
            )

    @pytest.mark.asyncio
    async def test_soft_deleted_document_raises_not_found(self):
        from datetime import datetime, timezone

        document = _document(deleted_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
        session = _FakeSession(_version(), document)
        with pytest.raises(NotFoundError):
            await AuthorizationService.authorize_document_version(
                _user(), "ver-1", session  # type: ignore[arg-type]
            )

    @pytest.mark.asyncio
    async def test_private_document_non_owner_raises_not_found(self):
        document = _document(access_level="private", owner_id="owner-2")
        session = _FakeSession(_version(), document)
        with pytest.raises(NotFoundError):
            await AuthorizationService.authorize_document_version(
                _user(), "ver-1", session  # type: ignore[arg-type]
            )
