"""
Integration tests for the repository layer.

Verifies:
  - Organization and User CRUD round-trips
  - Tenant isolation: users from org B are not returned when querying org A
  - Repository methods that require organization_id enforce it structurally
  - Soft delete behaves correctly
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.user_repository import OrganizationRepository, UserRepository


# ─── Organization ─────────────────────────────────────────────────────────────

@pytest.mark.integration
async def test_create_and_fetch_organization(db_session: AsyncSession):
    """OrganizationRepository can create and retrieve an organization."""
    repo = OrganizationRepository(db_session)

    org = await repo.create(name="Acme Corp", slug="acme-corp-test-001")
    assert org.id is not None
    assert org.slug == "acme-corp-test-001"

    fetched = await repo.get_by_id(org.id)
    assert fetched is not None
    assert fetched.name == "Acme Corp"


@pytest.mark.integration
async def test_get_organization_by_slug(db_session: AsyncSession):
    """OrganizationRepository.get_by_slug returns the correct organization."""
    repo = OrganizationRepository(db_session)

    org = await repo.create(name="Globex Inc", slug="globex-inc-test-001")
    fetched = await repo.get_by_slug("globex-inc-test-001")

    assert fetched is not None
    assert fetched.id == org.id


@pytest.mark.integration
async def test_get_organization_by_slug_missing_returns_none(db_session: AsyncSession):
    """get_by_slug returns None for a non-existent slug."""
    repo = OrganizationRepository(db_session)
    result = await repo.get_by_slug("definitely-does-not-exist-xyz")
    assert result is None


# ─── User ─────────────────────────────────────────────────────────────────────

@pytest.mark.integration
async def test_create_and_fetch_user(db_session: AsyncSession):
    """UserRepository can create and retrieve a user within an org."""
    org_repo = OrganizationRepository(db_session)
    user_repo = UserRepository(db_session)

    org = await org_repo.create(name="Tenant A", slug="tenant-a-repo-test-001")
    user = await user_repo.create(
        organization_id=org.id,
        email="alice@tenant-a.com",
        full_name="Alice Smith",
        password_hash="$argon2id$...",
    )

    assert user.id is not None
    assert user.email == "alice@tenant-a.com"

    fetched = await user_repo.get_by_id_for_org(user.id, org.id)
    assert fetched is not None
    assert fetched.full_name == "Alice Smith"


@pytest.mark.integration
async def test_email_stored_lowercase(db_session: AsyncSession):
    """User email is stored in lowercase."""
    org_repo = OrganizationRepository(db_session)
    user_repo = UserRepository(db_session)

    org = await org_repo.create(name="Case Org", slug="case-org-test-001")
    user = await user_repo.create(
        organization_id=org.id,
        email="BOB@EXAMPLE.COM",
        full_name="Bob Jones",
    )

    assert user.email == "bob@example.com"


@pytest.mark.integration
async def test_tenant_isolation_get_by_id(db_session: AsyncSession):
    """Fetching a user from org A with org B's organization_id returns None."""
    org_repo = OrganizationRepository(db_session)
    user_repo = UserRepository(db_session)

    org_a = await org_repo.create(name="Org A", slug="org-a-isolation-test-001")
    org_b = await org_repo.create(name="Org B", slug="org-b-isolation-test-001")

    user_a = await user_repo.create(
        organization_id=org_a.id,
        email="user@org-a.com",
        full_name="User A",
    )

    # Fetching org A's user with org B's id should return None — not the user
    result = await user_repo.get_by_id_for_org(user_a.id, org_b.id)
    assert result is None, (
        "Tenant isolation breach: get_by_id_for_org returned a user from another org"
    )


@pytest.mark.integration
async def test_tenant_isolation_list(db_session: AsyncSession):
    """list_for_org only returns users belonging to the specified org."""
    org_repo = OrganizationRepository(db_session)
    user_repo = UserRepository(db_session)

    org_a = await org_repo.create(name="List Org A", slug="list-org-a-test-001")
    org_b = await org_repo.create(name="List Org B", slug="list-org-b-test-001")

    await user_repo.create(
        organization_id=org_a.id, email="u1@org-a.com", full_name="U1"
    )
    await user_repo.create(
        organization_id=org_a.id, email="u2@org-a.com", full_name="U2"
    )
    await user_repo.create(
        organization_id=org_b.id, email="u3@org-b.com", full_name="U3"
    )

    org_a_users = await user_repo.list_for_org(org_a.id)
    org_b_users = await user_repo.list_for_org(org_b.id)

    assert len(org_a_users) == 2
    assert len(org_b_users) == 1
    # Verify no cross-org leakage
    org_a_emails = {u.email for u in org_a_users}
    assert "u3@org-b.com" not in org_a_emails


@pytest.mark.integration
async def test_get_by_email_for_org(db_session: AsyncSession):
    """UserRepository.get_by_email_for_org finds users correctly."""
    org_repo = OrganizationRepository(db_session)
    user_repo = UserRepository(db_session)

    org = await org_repo.create(name="Email Org", slug="email-org-test-001")
    await user_repo.create(
        organization_id=org.id, email="carol@example.com", full_name="Carol"
    )

    found = await user_repo.get_by_email_for_org("carol@example.com", org.id)
    assert found is not None
    assert found.full_name == "Carol"

    not_found = await user_repo.get_by_email_for_org("nobody@example.com", org.id)
    assert not_found is None


@pytest.mark.integration
async def test_soft_delete_excludes_user_from_queries(db_session: AsyncSession):
    """Soft-deleted users are excluded from list_for_org by default."""
    org_repo = OrganizationRepository(db_session)
    user_repo = UserRepository(db_session)

    org = await org_repo.create(name="Soft Delete Org", slug="soft-delete-org-test-001")
    user = await user_repo.create(
        organization_id=org.id, email="dave@example.com", full_name="Dave"
    )

    # Soft-delete the user
    await user_repo.soft_delete(user)

    # Should not appear in default list
    active_users = await user_repo.list_for_org(org.id)
    assert all(u.id != user.id for u in active_users)

    # Should appear in list when include_inactive=True
    all_users = await user_repo.list_for_org(org.id, include_inactive=True)
    assert any(u.id == user.id for u in all_users)
